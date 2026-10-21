//! Native schema evaluation. Python builds immutable nodes; all recursive
//! traversal, built-in checks, and issue aggregation are performed here.
//! Values stay as Python objects to preserve arbitrary precision and identity.

use std::cell::RefCell;
use std::collections::HashSet;
use std::net::Ipv6Addr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyException, PyRecursionError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTraceback, PyTuple, PyType,
};
use serde_json::Value;

const MAX_DEPTH: usize = 256;
static ASYNC_TOKEN_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Default)]
struct LazyState {
    cache: Vec<(usize, Py<NativeSchema>)>,
    last: Option<(usize, Py<NativeSchema>)>,
}

trait LazyStore {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>>;
    fn set_last(&self, key: usize, node: Py<NativeSchema>);
    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>>;
    fn push(&self, key: usize, node: Py<NativeSchema>);
}

impl LazyStore for RefCell<LazyState> {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.borrow();
        store
            .last
            .as_ref()
            .and_then(|(cached_key, node)| (*cached_key == key).then(|| node.clone_ref(py)))
    }

    fn set_last(&self, key: usize, node: Py<NativeSchema>) {
        self.borrow_mut().last = Some((key, node));
    }

    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.borrow();
        store
            .cache
            .iter()
            .rev()
            .find(|(cached_key, _)| *cached_key == key)
            .map(|(_, node)| node.clone_ref(py))
    }

    fn push(&self, key: usize, node: Py<NativeSchema>) {
        self.borrow_mut().cache.push((key, node));
    }
}

enum LazyHandle {
    Local(RefCell<LazyState>),
    Shared(Arc<Mutex<LazyState>>),
}

impl LazyStore for LazyHandle {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        match self {
            Self::Local(store) => store.last(py, key),
            Self::Shared(store) => store.last(py, key),
        }
    }

    fn set_last(&self, key: usize, node: Py<NativeSchema>) {
        match self {
            Self::Local(store) => store.set_last(key, node),
            Self::Shared(store) => store.set_last(key, node),
        }
    }

    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        match self {
            Self::Local(store) => store.find(py, key),
            Self::Shared(store) => store.find(py, key),
        }
    }

    fn push(&self, key: usize, node: Py<NativeSchema>) {
        match self {
            Self::Local(store) => store.push(key, node),
            Self::Shared(store) => store.push(key, node),
        }
    }
}

impl LazyStore for Mutex<LazyState> {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.lock().unwrap();
        store
            .last
            .as_ref()
            .and_then(|(cached_key, node)| (*cached_key == key).then(|| node.clone_ref(py)))
    }

    fn set_last(&self, key: usize, node: Py<NativeSchema>) {
        self.lock().unwrap().last = Some((key, node));
    }

    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.lock().unwrap();
        store
            .cache
            .iter()
            .rev()
            .find(|(cached_key, _)| *cached_key == key)
            .map(|(_, node)| node.clone_ref(py))
    }

    fn push(&self, key: usize, node: Py<NativeSchema>) {
        self.lock().unwrap().cache.push((key, node));
    }
}

enum PathPart {
    Value(Py<PyAny>),
    Index(usize),
}

impl PathPart {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Value(value) => Self::Value(value.clone_ref(py)),
            Self::Index(index) => Self::Index(*index),
        }
    }

    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::Value(value) => Ok(value.clone_ref(py)),
            Self::Index(index) => Ok((*index).into_pyobject(py)?.into_any().unbind()),
        }
    }

    fn visit(&self, visit: &PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        if let Self::Value(value) = self {
            visit.call(value)?;
        }
        Ok(())
    }
}

type Path = Vec<PathPart>;
type Result<T> = std::result::Result<T, Error>;

struct Issue {
    code: Arc<str>,
    message: Arc<str>,
    path: Path,
    branches: Vec<Vec<Issue>>,
    params: Py<PyAny>,
}

struct ErrorTemplate {
    code: Arc<str>,
    message: Arc<str>,
    params: Py<PyAny>,
}

enum Error {
    Invalid(Vec<Issue>),
    Python(PyErr),
}

impl From<PyErr> for Error {
    fn from(error: PyErr) -> Self {
        Self::Python(error)
    }
}

impl From<std::convert::Infallible> for Error {
    fn from(never: std::convert::Infallible) -> Self {
        match never {}
    }
}

impl Issue {
    fn new(
        py: Python<'_>,
        code: &str,
        message: &str,
        path: &Path,
        params: Bound<'_, PyAny>,
    ) -> Self {
        Self {
            code: Arc::from(code),
            message: Arc::from(message),
            path: path.iter().map(|part| part.clone_ref(py)).collect(),
            branches: Vec::new(),
            params: params.unbind(),
        }
    }

    fn from_template(py: Python<'_>, template: &ErrorTemplate, path: &Path) -> Self {
        Self {
            code: template.code.clone(),
            message: template.message.clone(),
            path: path.iter().map(|part| part.clone_ref(py)).collect(),
            branches: Vec::new(),
            params: template.params.clone_ref(py),
        }
    }

    fn simple(py: Python<'_>, code: &str, message: &str, path: &Path) -> Self {
        Self::new(py, code, message, path, PyDict::new(py).into_any())
    }

    fn to_raw(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let path = PyTuple::new(
            py,
            self.path
                .iter()
                .map(|part| part.to_py(py))
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let branches = self
            .branches
            .iter()
            .map(|branch| {
                let issues = branch
                    .iter()
                    .map(|i| i.to_raw(py))
                    .collect::<PyResult<Vec<_>>>()?;
                Ok(PyTuple::new(py, issues)?.into_any().unbind())
            })
            .collect::<PyResult<Vec<Py<PyAny>>>>()?;
        Ok((
            self.code.as_ref(),
            self.message.as_ref(),
            path,
            PyTuple::new(py, branches)?,
            self.params.bind(py),
        )
            .into_pyobject(py)?
            .into_any()
            .unbind())
    }

    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let path = value
            .getattr("path")?
            .try_iter()?
            .map(|v| v.map(|part| PathPart::Value(part.unbind())))
            .collect::<PyResult<Vec<_>>>()?;
        let branches = value
            .getattr("branches")?
            .try_iter()?
            .map(|branch| {
                branch?
                    .try_iter()?
                    .map(|issue| Self::from_python(&issue?))
                    .collect::<PyResult<Vec<_>>>()
            })
            .collect::<PyResult<Vec<_>>>()?;
        let params = value
            .getattr("params")
            .unwrap_or_else(|_| PyDict::new(value.py()).into_any())
            .unbind();
        Ok(Self {
            code: Arc::from(value.getattr("code")?.extract::<String>()?),
            message: Arc::from(value.getattr("message")?.extract::<String>()?),
            path,
            branches,
            params,
        })
    }
}

#[pyclass(module = "zodish._native")]
struct NativeIssueBundle {
    issues: Vec<Issue>,
}

#[pymethods]
impl NativeIssueBundle {
    fn to_raw(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let issues = self
            .issues
            .iter()
            .map(|issue| issue.to_raw(py))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(PyTuple::new(py, issues)?.into_any().unbind())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        for issue in &self.issues {
            visit_issue(issue, &visit)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.issues.clear();
    }
}

fn visit_issue(issue: &Issue, visit: &PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
    for part in &issue.path {
        part.visit(visit)?;
    }
    visit.call(&issue.params)?;
    for branch in &issue.branches {
        for child in branch {
            visit_issue(child, visit)?;
        }
    }
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum AsyncSessionState {
    Idle,
    Running,
    Awaiting,
    Done,
    Closed,
}

enum AsyncTask {
    Eval {
        node: Py<NativeSchema>,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    },
    Deliver {
        outcome: Result<Py<PyAny>>,
        path: Path,
    },
}

enum AsyncNext {
    Task(AsyncTask),
    Suspend,
    Done(Py<PyAny>),
    Error(Error),
}

#[derive(Clone, Copy)]
enum AsyncPendingKind {
    Refine,
    SuperRefine,
    Transform,
    Overwrite,
    CatchFallback,
    Preprocess,
    DefaultFactory,
}

struct AsyncPending {
    node: Py<NativeSchema>,
    kind: AsyncPendingKind,
    parsed: Option<Py<PyAny>>,
    child: Option<Py<NativeSchema>>,
    path: Path,
    depth: usize,
}

struct AsyncCallbackFrame {
    node: Py<NativeSchema>,
    path: Path,
}

struct CatchFrame {
    node: Py<NativeSchema>,
    fallback: Py<PyAny>,
    path: Path,
    asynchronous: bool,
}

struct RefineFrame {
    node: Py<NativeSchema>,
    path: Path,
}

struct SuperRefineFrame {
    node: Py<NativeSchema>,
    path: Path,
}

struct PostChildFrame {
    node: Py<NativeSchema>,
    path: Path,
}

struct StringBoolFrame {
    node: Py<NativeSchema>,
    path: Path,
}

struct PipeFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    stage: u8,
}

struct ArrayFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    items: Vec<Py<PyAny>>,
    index: usize,
    result: Vec<Py<PyAny>>,
    issues: Vec<Issue>,
    is_tuple: bool,
    fixed: usize,
    rest: Option<Py<NativeSchema>>,
}

struct ObjectFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    keys: Vec<Py<PyAny>>,
    values: Vec<Option<Py<PyAny>>>,
    fast: Vec<bool>,
    cursor: usize,
    result: Py<PyDict>,
    issues: Vec<Issue>,
    extras: Vec<(Py<PyAny>, Py<PyAny>)>,
    extra_cursor: usize,
    extras_phase: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RecordStage {
    Key,
    KeyWaiting,
    Value,
    ValueWaiting,
}

struct RecordFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    entries: Vec<(Py<PyAny>, Py<PyAny>)>,
    index: usize,
    stage: RecordStage,
    parsed_key: Option<Py<PyAny>>,
    result: Py<PyDict>,
    issues: Vec<Issue>,
    key_node: Option<Py<NativeSchema>>,
}

struct UnionFrame {
    node: Py<NativeSchema>,
    value: Py<PyAny>,
    path: Path,
    depth: usize,
    index: usize,
    branches: Vec<Vec<Issue>>,
}

struct StoredPythonError {
    value: Py<PyAny>,
    traceback: Option<Py<PyTraceback>>,
}

enum StoredOutcome {
    Value(Py<PyAny>),
    Invalid(Vec<Issue>),
    Python(StoredPythonError),
}

impl StoredOutcome {
    fn store(py: Python<'_>, outcome: Result<Py<PyAny>>) -> Self {
        match outcome {
            Ok(value) => Self::Value(value),
            Err(Error::Invalid(issues)) => Self::Invalid(issues),
            Err(Error::Python(error)) => {
                let traceback = error.traceback(py).map(Bound::unbind);
                Self::Python(StoredPythonError {
                    value: error.into_value(py).into_any(),
                    traceback,
                })
            }
        }
    }

    fn restore(self, py: Python<'_>) -> Result<Py<PyAny>> {
        match self {
            Self::Value(value) => Ok(value),
            Self::Invalid(issues) => Err(Error::Invalid(issues)),
            Self::Python(error) => {
                let restored = PyErr::from_value(error.value.bind(py).clone());
                if let Some(traceback) = error.traceback {
                    restored.set_traceback(py, Some(traceback.bind(py).clone()));
                }
                Err(Error::Python(restored))
            }
        }
    }
}

struct IntersectionFrame {
    node: Py<NativeSchema>,
    value: Py<PyAny>,
    path: Path,
    depth: usize,
    stage: u8,
    left: Option<StoredOutcome>,
}

// A suspended session owns a stack of frames. Nodes whose `has_async` flag is
// false are still executed by `NativeSchema::validate`; frames exist only for
// parents that must resume after an async descendant and for the async leaf
// callbacks themselves.
enum AsyncFrame {
    AsyncCallback(AsyncCallbackFrame),
    Catch(CatchFrame),
    Refine(RefineFrame),
    SuperRefine(SuperRefineFrame),
    PostChild(PostChildFrame),
    StringBool(StringBoolFrame),
    Pipe(PipeFrame),
    Array(ArrayFrame),
    Object(ObjectFrame),
    Record(RecordFrame),
    Union(UnionFrame),
    Intersection(IntersectionFrame),
}

#[pyclass(module = "zodish._native", weakref)]
struct AsyncSession {
    node: Option<Py<NativeSchema>>,
    value: Option<Py<PyAny>>,
    missing: Option<Py<PyAny>>,
    issue_type: Option<Py<PyAny>>,
    error_type: Option<Py<PyAny>>,
    frames: Vec<AsyncFrame>,
    pending: Option<AsyncPending>,
    awaitable: Option<Py<PyAny>>,
    token: u64,
    thread: Option<std::thread::ThreadId>,
    state: AsyncSessionState,
    lazy_store: Arc<Mutex<LazyState>>,
}

#[pymethods]
impl AsyncSession {
    #[new]
    fn new(
        node: Py<NativeSchema>,
        value: Py<PyAny>,
        missing: Py<PyAny>,
        issue_type: Py<PyAny>,
        error_type: Py<PyAny>,
    ) -> Self {
        Self {
            node: Some(node),
            value: Some(value),
            missing: Some(missing),
            issue_type: Some(issue_type),
            error_type: Some(error_type),
            frames: Vec::new(),
            pending: None,
            awaitable: None,
            token: 0,
            thread: None,
            state: AsyncSessionState::Idle,
            lazy_store: Arc::new(Mutex::new(LazyState::default())),
        }
    }

    fn start(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.state != AsyncSessionState::Idle {
            return Err(PyRuntimeError::new_err("Async session has already started"));
        }
        self.state = AsyncSessionState::Running;
        self.thread = Some(std::thread::current().id());
        self.frames.clear();
        self.pending = None;
        self.awaitable = None;
        self.lazy_store.lock().unwrap().cache.clear();
        self.lazy_store.lock().unwrap().last = None;
        let node = self
            .node
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
            .clone_ref(py);
        let value = self
            .value
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("Async session has no input"))?;
        let missing = self
            .missing
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
            .clone_ref(py);
        let issue_type = self
            .issue_type
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
            .clone_ref(py);
        let error_type = self
            .error_type
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
            .clone_ref(py);
        let has_async = node.bind(py).try_borrow().map_err(PyErr::from)?.has_async;
        if !has_async {
            let parsed = node.bind(py).borrow().parse(
                py,
                value.bind(py).clone(),
                missing.bind(py).clone(),
                issue_type.bind(py).clone(),
                error_type.bind(py).clone(),
            );
            return match parsed {
                Ok(value) => {
                    self.state = AsyncSessionState::Done;
                    self.clear_refs();
                    Ok((true, value).into_pyobject(py)?.into_any().unbind())
                }
                Err(error) => {
                    self.state = AsyncSessionState::Closed;
                    self.clear_refs();
                    Err(error)
                }
            };
        }
        self.continue_run(
            py,
            AsyncNext::Task(AsyncTask::Eval {
                node,
                value,
                path: Vec::new(),
                depth: 0,
            }),
        )
    }

    fn resume_value(
        &mut self,
        py: Python<'_>,
        token: u64,
        value: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        self.check_awaiting(token)?;
        let pending = self
            .pending
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("Async session has no pending callback"))?;
        self.awaitable = None;
        self.state = AsyncSessionState::Running;
        let next = self.resume_callback(py, pending, value)?;
        self.continue_run(py, next)
    }

    fn resume_error(
        &mut self,
        py: Python<'_>,
        token: u64,
        error: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        self.check_awaiting(token)?;
        let pending = self
            .pending
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("Async session has no pending callback"))?;
        self.awaitable = None;
        let exception = error.bind(py);
        if !exception.is_instance_of::<PyException>() {
            self.state = AsyncSessionState::Closed;
            self.clear_refs();
            return Err(PyErr::from_value(exception.clone()));
        }
        self.state = AsyncSessionState::Running;
        let ctx = self.context(py)?;
        let outcome = Err(ctx.normalize(
            Error::Python(PyErr::from_value(exception.clone())),
            &pending.path,
        ));
        self.continue_run(
            py,
            AsyncNext::Task(AsyncTask::Deliver {
                outcome,
                path: pending.path,
            }),
        )
    }

    fn close(&mut self, py: Python<'_>) {
        if let Some(awaitable) = self.awaitable.take() {
            let bound = awaitable.bind(py);
            let is_coroutine = py
                .import("inspect")
                .and_then(|inspect| inspect.call_method1("iscoroutine", (bound,)))
                .and_then(|value| value.is_truthy())
                .unwrap_or(false);
            if is_coroutine {
                let _ = bound.call_method0("close");
            }
        }
        self.state = AsyncSessionState::Closed;
        self.clear_refs();
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        if let Some(node) = &self.node {
            visit.call(node)?;
        }
        for value in [
            &self.value,
            &self.missing,
            &self.issue_type,
            &self.error_type,
            &self.awaitable,
        ]
        .into_iter()
        .flatten()
        {
            visit.call(value)?;
        }
        if let Some(pending) = &self.pending {
            visit.call(&pending.node)?;
            if let Some(parsed) = &pending.parsed {
                visit.call(parsed)?;
            }
            if let Some(child) = &pending.child {
                visit.call(child)?;
            }
            for part in &pending.path {
                part.visit(&visit)?;
            }
        }
        for frame in &self.frames {
            visit_async_frame(frame, &visit)?;
        }
        if let Ok(store) = self.lazy_store.lock() {
            for (_, node) in store.cache.iter() {
                visit.call(node)?;
            }
            if let Some((_, node)) = store.last.as_ref() {
                visit.call(node)?;
            }
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.state = AsyncSessionState::Closed;
        self.clear_refs();
    }
}

impl AsyncSession {
    fn context<'py>(&self, py: Python<'py>) -> PyResult<Context<'py>> {
        Ok(Context {
            py,
            missing: self
                .missing
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
                .bind(py)
                .clone(),
            issue_type: self
                .issue_type
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
                .bind(py)
                .clone(),
            error_type: self
                .error_type
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
                .bind(py)
                .clone(),
            lazy_store: LazyHandle::Shared(self.lazy_store.clone()),
        })
    }

    fn check_awaiting(&self, token: u64) -> PyResult<()> {
        if self.state != AsyncSessionState::Awaiting {
            return Err(PyRuntimeError::new_err(
                "Async session is not awaiting a callback",
            ));
        }
        if self.thread != Some(std::thread::current().id()) {
            return Err(PyRuntimeError::new_err(
                "Async session resumed from a different thread",
            ));
        }
        if token != self.token {
            return Err(PyRuntimeError::new_err("Stale async session token"));
        }
        Ok(())
    }

    fn clear_refs(&mut self) {
        self.node = None;
        self.value = None;
        self.missing = None;
        self.issue_type = None;
        self.error_type = None;
        self.frames.clear();
        self.pending = None;
        self.awaitable = None;
        self.lazy_store.lock().unwrap().cache.clear();
        self.lazy_store.lock().unwrap().last = None;
        self.thread = None;
    }

    fn continue_run(&mut self, py: Python<'_>, next: AsyncNext) -> PyResult<Py<PyAny>> {
        let next = match next {
            AsyncNext::Task(task) => self.run(py, task)?,
            other => other,
        };
        self.finish(py, next)
    }

    fn finish(&mut self, py: Python<'_>, next: AsyncNext) -> PyResult<Py<PyAny>> {
        match next {
            AsyncNext::Suspend => {
                self.state = AsyncSessionState::Awaiting;
                let awaitable = self
                    .awaitable
                    .as_ref()
                    .ok_or_else(|| PyRuntimeError::new_err("Async session has no awaitable"))?
                    .clone_ref(py);
                let payload = (self.token, awaitable)
                    .into_pyobject(py)?
                    .into_any()
                    .unbind();
                Ok((false, payload).into_pyobject(py)?.into_any().unbind())
            }
            AsyncNext::Done(value) => {
                self.state = AsyncSessionState::Done;
                self.clear_refs();
                Ok((true, value).into_pyobject(py)?.into_any().unbind())
            }
            AsyncNext::Error(error) => {
                let ctx = self.context(py)?;
                let raised = ctx.make_exception(error);
                self.state = AsyncSessionState::Closed;
                self.clear_refs();
                Err(raised)
            }
            AsyncNext::Task(_) => Err(PyRuntimeError::new_err(
                "Async session stopped with a pending task",
            )),
        }
    }

    fn run(&mut self, py: Python<'_>, mut task: AsyncTask) -> PyResult<AsyncNext> {
        loop {
            match task {
                AsyncTask::Eval {
                    node,
                    value,
                    path,
                    depth,
                } => {
                    task = match self.eval_node(py, node, value, path, depth)? {
                        AsyncNext::Task(task) => task,
                        other => return Ok(other),
                    };
                }
                AsyncTask::Deliver { outcome, path } => {
                    let ctx = self.context(py)?;
                    let outcome = match outcome {
                        Ok(value) => Ok(value),
                        Err(error) => Err(ctx.normalize(error, &path)),
                    };
                    if let Err(Error::Python(error)) = &outcome {
                        if !error.is_instance_of::<PyException>(py) {
                            if let Err(error) = outcome {
                                return Ok(AsyncNext::Error(error));
                            }
                            unreachable!();
                        }
                    }
                    task = match self.deliver(py, outcome)? {
                        AsyncNext::Task(task) => task,
                        other => return Ok(other),
                    };
                }
            }
        }
    }

    fn deliver(&mut self, py: Python<'_>, outcome: Result<Py<PyAny>>) -> PyResult<AsyncNext> {
        match self.frames.pop() {
            Some(frame) => self.resume_frame(py, frame, outcome),
            None => Ok(match outcome {
                Ok(value) => AsyncNext::Done(value),
                Err(error) => AsyncNext::Error(error),
            }),
        }
    }
}

impl AsyncSession {
    fn eval_node(
        &mut self,
        py: Python<'_>,
        node: Py<NativeSchema>,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        if depth >= MAX_DEPTH {
            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: Err(
                    PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into(),
                ),
                path,
            }));
        }
        let ctx = self.context(py)?;
        let node_ref = node.bind(py).try_borrow().map_err(PyErr::from)?;
        if !node_ref.has_async {
            let result = node_ref.validate(&ctx, value.bind(py), &mut clone_path(py, &path), depth);
            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: result,
                path,
            }));
        }
        let kind = node_ref.kind;
        let missing = value.bind(py).is(&ctx.missing);
        match kind {
            Kind::Cleared => Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: Err(PyRuntimeError::new_err("Native schema has been cleared").into()),
                path,
            })),
            Kind::AsyncRefine
            | Kind::AsyncSuperRefine
            | Kind::AsyncTransform
            | Kind::AsyncOverwrite => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames
                    .push(AsyncFrame::AsyncCallback(AsyncCallbackFrame {
                        node: node.clone_ref(py),
                        path: clone_path(py, &path),
                    }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::AsyncPreprocess => {
                let child = node_ref.children[0].clone_ref(py);
                let callback = node_ref.args[0].clone_ref(py);
                drop(node_ref);
                let result = match callback.bind(py).call1((value.bind(py),)) {
                    Ok(result) => result,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let awaitable = match check_awaitable(py, &result) {
                    Ok(awaitable) => awaitable,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(error),
                            path,
                        }));
                    }
                };
                self.token = ASYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
                self.pending = Some(AsyncPending {
                    node: node.clone_ref(py),
                    kind: AsyncPendingKind::Preprocess,
                    parsed: None,
                    child: Some(child),
                    path: clone_path(py, &path),
                    depth,
                });
                self.awaitable = Some(awaitable);
                Ok(AsyncNext::Suspend)
            }
            Kind::AsyncDefaultFactory => {
                let child = node_ref.children[0].clone_ref(py);
                if !missing {
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value,
                        path,
                        depth: depth + 1,
                    }));
                }
                let callback = node_ref.args[0].clone_ref(py);
                drop(node_ref);
                let result = match callback.bind(py).call0() {
                    Ok(result) => result,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let awaitable = match check_awaitable(py, &result) {
                    Ok(awaitable) => awaitable,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(error),
                            path,
                        }));
                    }
                };
                self.token = ASYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
                self.pending = Some(AsyncPending {
                    node: node.clone_ref(py),
                    kind: AsyncPendingKind::DefaultFactory,
                    parsed: None,
                    child: Some(child),
                    path: clone_path(py, &path),
                    depth,
                });
                self.awaitable = Some(awaitable);
                Ok(AsyncNext::Suspend)
            }
            Kind::AsyncCatch => {
                let child = node_ref.children[0].clone_ref(py);
                let fallback = node_ref.args[0].clone_ref(py);
                self.frames.push(AsyncFrame::Catch(CatchFrame {
                    node: node.clone_ref(py),
                    fallback,
                    path: clone_path(py, &path),
                    asynchronous: true,
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Optional if missing => {
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Deliver {
                    outcome: Ok(value),
                    path,
                }))
            }
            Kind::Nullable if value.bind(py).is_none() => {
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Deliver {
                    outcome: Ok(value),
                    path,
                }))
            }
            Kind::Optional | Kind::Nullable => {
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::NonOptional => {
                if missing {
                    let outcome = ctx.fail("required", "Required", &path);
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                }
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Default | Kind::DefaultFactory => {
                let child = node_ref.children[0].clone_ref(py);
                let arg = node_ref.args[0].clone_ref(py);
                drop(node_ref);
                let value = if missing {
                    if matches!(kind, Kind::Default) {
                        match deepcopy(arg.bind(py)) {
                            Ok(value) => value.unbind(),
                            Err(error) => {
                                return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                    outcome: Err(Error::Python(error)),
                                    path,
                                }));
                            }
                        }
                    } else {
                        match arg.bind(py).call0() {
                            Ok(value) => value.unbind(),
                            Err(error) => {
                                return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                    outcome: Err(Error::Python(error)),
                                    path,
                                }));
                            }
                        }
                    }
                } else {
                    value
                };
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Lazy => {
                let key = &*node_ref as *const NativeSchema as usize;
                let resolved = match ctx.lazy_store.last(py, key) {
                    Some(node) => node,
                    None => match ctx.lazy_store.find(py, key) {
                        Some(node) => {
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            node
                        }
                        None => {
                            let factory = node_ref.args[0].clone_ref(py);
                            drop(node_ref);
                            let schema = match factory.bind(py).call0() {
                                Ok(schema) => schema,
                                Err(error) => {
                                    return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                        outcome: Err(Error::Python(error)),
                                        path,
                                    }));
                                }
                            };
                            let schema = match schema.getattr("_node") {
                                Ok(schema) => schema,
                                Err(error) => {
                                    return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                        outcome: Err(Error::Python(error)),
                                        path,
                                    }));
                                }
                            };
                            let node = match schema.cast_into::<NativeSchema>() {
                                Ok(node) => node.unbind(),
                                Err(error) => {
                                    return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                        outcome: Err(Error::Python(error.into())),
                                        path,
                                    }));
                                }
                            };
                            ctx.lazy_store.push(key, node.clone_ref(py));
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            return Ok(AsyncNext::Task(AsyncTask::Eval {
                                node,
                                value,
                                path,
                                depth: depth + 1,
                            }));
                        }
                    },
                };
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: resolved,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Catch => {
                let child = node_ref.children[0].clone_ref(py);
                let fallback = node_ref.args[0].clone_ref(py);
                self.frames.push(AsyncFrame::Catch(CatchFrame {
                    node: node.clone_ref(py),
                    fallback,
                    path: clone_path(py, &path),
                    asynchronous: false,
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Coerce => {
                if missing {
                    let child = node_ref.children[0].clone_ref(py);
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value,
                        path,
                        depth: depth + 1,
                    }));
                }
                let target: String = match node_ref.args[0].bind(py).extract() {
                    Ok(target) => target,
                    Err(error) => {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let converted = match coerce_value(py, &target, value.bind(py)) {
                    Ok(Some(value)) => value,
                    Ok(None) => {
                        let expected = format!("coercible {target}");
                        let params = PyDict::new(py);
                        if let Err(error) = params.set_item("expected", &expected) {
                            drop(node_ref);
                            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                outcome: Err(Error::Python(error)),
                                path,
                            }));
                        }
                        let outcome = ctx.fail_with(
                            "invalid_type",
                            &format!("Expected {expected}"),
                            &path,
                            params,
                        );
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                    }
                    Err(error) => {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value: converted.unbind(),
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Preprocess => {
                let callback = node_ref.args[0].clone_ref(py);
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                let processed = match callback.bind(py).call1((value.bind(py),)) {
                    Ok(value) => value.unbind(),
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value: processed,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Refine => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::Refine(RefineFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::SuperRefine => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::SuperRefine(SuperRefineFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Transform
            | Kind::Overwrite
            | Kind::LengthMin
            | Kind::LengthMax
            | Kind::Min
            | Kind::Max
            | Kind::ExclusiveMin
            | Kind::ExclusiveMax
            | Kind::MultipleOf
            | Kind::Positive
            | Kind::Integral
            | Kind::Regex
            | Kind::StartsWith
            | Kind::EndsWith
            | Kind::Includes
            | Kind::Ipv6
            | Kind::Trim
            | Kind::Lower
            | Kind::Upper => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::PostChild(PostChildFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::StringBool => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::StringBool(StringBoolFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Pipe => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::Pipe(PipeFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                    depth,
                    stage: 0,
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Array | Kind::Tuple => {
                self.start_array(py, &ctx, node, &node_ref, value, path, depth)
            }
            Kind::Object => self.start_object(py, &ctx, node, &node_ref, value, path, depth),
            Kind::Record => self.start_record(py, &ctx, node, &node_ref, value, path, depth),
            Kind::Union => self.start_union(py, node, &node_ref, value, path, depth),
            Kind::Intersection => {
                self.start_intersection(py, &ctx, node, &node_ref, value, path, depth)
            }
            Kind::DiscriminatedUnion => {
                if missing {
                    drop(node_ref);
                    let outcome = ctx.fail("required", "Required", &path);
                    return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                }
                let Ok(dict) = value.bind(py).cast::<PyDict>() else {
                    let params = PyDict::new(py);
                    if let Err(error) = params.set_item("expected", "dict with string keys") {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                    let outcome = ctx.fail_with(
                        "invalid_type",
                        "Expected dict with string keys",
                        &path,
                        params,
                    );
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                };
                let key = node_ref.args[0].bind(py);
                let options = match node_ref.args[1].bind(py).cast::<PyTuple>() {
                    Ok(options) => options,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error.into())),
                            path,
                        }));
                    }
                };
                let raw = match dict.get_item(key) {
                    Ok(Some(raw)) => raw,
                    Ok(None) => {
                        let mut issue_path = clone_path(py, &path);
                        issue_path.push(PathPart::Value(node_ref.args[0].clone_ref(py)));
                        let outcome = ctx.fail("required", "Required", &issue_path);
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                    }
                    Err(error) => {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let mut selected = None;
                for (index, option) in options.iter().enumerate() {
                    if raw.get_type().is(option.get_type()) && raw.eq(&option)? {
                        selected = Some(index);
                        break;
                    }
                }
                let Some(index) = selected else {
                    let message = match enum_message(options) {
                        Ok(message) => message,
                        Err(error) => {
                            drop(node_ref);
                            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                outcome: Err(Error::Python(error)),
                                path,
                            }));
                        }
                    };
                    let params = PyDict::new(py);
                    if let Err(error) = params.set_item("values", options) {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                    let mut issue_path = clone_path(py, &path);
                    issue_path.push(PathPart::Value(node_ref.args[0].clone_ref(py)));
                    let outcome = ctx.fail_with("invalid_value", &message, &issue_path, params);
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                };
                let child = node_ref.children[index].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            _ => Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: Err(PyTypeError::new_err(
                    "Async schema composition is not supported for this operation",
                )
                .into()),
                path,
            })),
        }
    }

    fn resume_frame(
        &mut self,
        py: Python<'_>,
        frame: AsyncFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match frame {
            AsyncFrame::AsyncCallback(frame) => self.resume_async_callback(py, frame, outcome),
            AsyncFrame::Catch(frame) => {
                let path = frame.path;
                match outcome {
                    Ok(value) => Ok(deliver_at(Ok(value), path)),
                    Err(Error::Invalid(_)) if frame.asynchronous => {
                        let result = match frame.fallback.bind(py).call0() {
                            Ok(result) => result,
                            Err(error) => {
                                return Ok(deliver_at(Err(Error::Python(error)), path));
                            }
                        };
                        let awaitable = match check_awaitable(py, &result) {
                            Ok(awaitable) => awaitable,
                            Err(error) => {
                                return Ok(deliver_at(Err(error), path));
                            }
                        };
                        self.token = ASYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
                        self.pending = Some(AsyncPending {
                            node: frame.node,
                            kind: AsyncPendingKind::CatchFallback,
                            parsed: None,
                            child: None,
                            path: clone_path(py, &path),
                            depth: 0,
                        });
                        self.awaitable = Some(awaitable);
                        Ok(AsyncNext::Suspend)
                    }
                    Err(Error::Invalid(_)) => {
                        let fallback: Result<Py<PyAny>> = if frame.fallback.bind(py).is_callable() {
                            frame
                                .fallback
                                .bind(py)
                                .call0()
                                .map(Bound::unbind)
                                .map_err(Error::Python)
                        } else {
                            deepcopy(frame.fallback.bind(py))
                                .map(Bound::unbind)
                                .map_err(Error::Python)
                        };
                        Ok(deliver_at(fallback, path))
                    }
                    Err(error) => Ok(deliver_at(Err(error), path)),
                }
            }
            AsyncFrame::Refine(frame) => {
                let path = frame.path;
                match outcome {
                    Ok(parsed) => {
                        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                        let result = (|| -> Result<Py<PyAny>> {
                            let parsed_bound = parsed.bind(py);
                            if node.args[0].bind(py).call1((parsed_bound,))?.is_truthy()? {
                                return Ok(parsed_bound.clone().unbind());
                            }
                            let message: String = node.args[1].bind(py).extract()?;
                            let relative = node.args[2].bind(py);
                            let mut issue_path = clone_path(py, &path);
                            for part in relative.try_iter()? {
                                issue_path.push(PathPart::Value(part?.unbind()));
                            }
                            let ctx = self.context(py)?;
                            ctx.fail("custom", &message, &issue_path)
                        })();
                        drop(node);
                        Ok(deliver_at(result, path))
                    }
                    Err(error) => Ok(deliver_at(Err(error), path)),
                }
            }
            AsyncFrame::SuperRefine(frame) => {
                let path = frame.path;
                match outcome {
                    Ok(parsed) => {
                        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                        let result = (|| -> Result<Py<PyAny>> {
                            let ctx = self.context(py)?;
                            let parsed_bound = parsed.bind(py);
                            let result = node.args[0].bind(py).call1((parsed_bound,))?;
                            if result.is_none() {
                                return Ok(parsed_bound.clone().unbind());
                            }
                            let mut issues = Vec::new();
                            if result.is_instance(&ctx.issue_type)? {
                                issues.push(Issue::from_python(&result)?);
                            } else if result.is_instance_of::<PyList>()
                                || result.is_instance_of::<PyTuple>()
                            {
                                for item in result.try_iter()? {
                                    let item = item?;
                                    if !item.is_instance(&ctx.issue_type)? {
                                        return Err(PyTypeError::new_err(
                                            "super_refine callback returned a non-Issue value",
                                        )
                                        .into());
                                    }
                                    issues.push(Issue::from_python(&item)?);
                                }
                            } else {
                                return Err(PyTypeError::new_err(
                                    "super_refine callback must return Issue, a list or tuple of Issue, or None",
                                )
                                .into());
                            }
                            if issues.is_empty() {
                                return Ok(parsed_bound.clone().unbind());
                            }
                            for issue in &mut issues {
                                let mut absolute = clone_path(py, &path);
                                absolute.extend(issue.path.iter().map(|part| part.clone_ref(py)));
                                issue.path = absolute;
                            }
                            Err(Error::Invalid(issues))
                        })();
                        drop(node);
                        Ok(deliver_at(result, path))
                    }
                    Err(error) => Ok(deliver_at(Err(error), path)),
                }
            }
            AsyncFrame::PostChild(frame) => {
                let path = frame.path;
                match outcome {
                    Ok(parsed) => {
                        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                        let ctx = self.context(py)?;
                        let result = node.evaluate_post_child(&ctx, parsed.bind(py), &path);
                        drop(node);
                        Ok(deliver_at(result, path))
                    }
                    Err(error) => Ok(deliver_at(Err(error), path)),
                }
            }
            AsyncFrame::StringBool(frame) => {
                let path = frame.path;
                match outcome {
                    Ok(parsed) => {
                        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                        let ctx = self.context(py)?;
                        let result = (|| -> Result<Py<PyAny>> {
                            let parsed = parsed.bind(py);
                            let text: String = parsed.extract()?;
                            let case_sensitive: bool = node.args[2].bind(py).extract()?;
                            let normalized = if case_sensitive {
                                text
                            } else {
                                text.to_lowercase()
                            };
                            for item in node.args[0].bind(py).try_iter()? {
                                let candidate: String = item?.extract()?;
                                let candidate = if case_sensitive {
                                    candidate
                                } else {
                                    candidate.to_lowercase()
                                };
                                if normalized == candidate {
                                    return Ok(true
                                        .into_pyobject(py)?
                                        .to_owned()
                                        .into_any()
                                        .unbind());
                                }
                            }
                            for item in node.args[1].bind(py).try_iter()? {
                                let candidate: String = item?.extract()?;
                                let candidate = if case_sensitive {
                                    candidate
                                } else {
                                    candidate.to_lowercase()
                                };
                                if normalized == candidate {
                                    return Ok(false
                                        .into_pyobject(py)?
                                        .to_owned()
                                        .into_any()
                                        .unbind());
                                }
                            }
                            let message: String = node.args[3].bind(py).extract()?;
                            let truthy = node.args[0]
                                .bind(py)
                                .cast::<PyTuple>()
                                .map_err(PyErr::from)?;
                            let falsy = node.args[1]
                                .bind(py)
                                .cast::<PyTuple>()
                                .map_err(PyErr::from)?;
                            let values = truthy
                                .iter()
                                .chain(falsy.iter())
                                .map(Bound::unbind)
                                .collect::<Vec<_>>();
                            let values = PyTuple::new(py, values)?;
                            let params = PyDict::new(py);
                            params.set_item("values", values)?;
                            ctx.fail_with("invalid_value", &message, &path, params)
                        })();
                        drop(node);
                        Ok(deliver_at(result, path))
                    }
                    Err(error) => Ok(deliver_at(Err(error), path)),
                }
            }
            AsyncFrame::Pipe(frame) => {
                let path = clone_path(py, &frame.path);
                match frame.stage {
                    0 => match outcome {
                        Ok(parsed) => {
                            let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                            let child = node.children[1].clone_ref(py);
                            drop(node);
                            let depth = frame.depth;
                            let mut frame = frame;
                            frame.stage = 1;
                            self.frames.push(AsyncFrame::Pipe(frame));
                            Ok(AsyncNext::Task(AsyncTask::Eval {
                                node: child,
                                value: parsed,
                                path,
                                depth: depth + 1,
                            }))
                        }
                        Err(error) => Ok(deliver_at(Err(error), path)),
                    },
                    _ => Ok(deliver_at(outcome, path)),
                }
            }
            AsyncFrame::Array(frame) => self.resume_array(py, frame, outcome),
            AsyncFrame::Object(frame) => self.resume_object(py, frame, outcome),
            AsyncFrame::Record(frame) => self.resume_record(py, frame, outcome),
            AsyncFrame::Union(frame) => self.resume_union(py, frame, outcome),
            AsyncFrame::Intersection(frame) => self.resume_intersection(py, frame, outcome),
        }
    }

    fn advance(&mut self, py: Python<'_>) -> PyResult<AsyncNext> {
        let frame = self
            .frames
            .pop()
            .ok_or_else(|| PyRuntimeError::new_err("Async session has no frame"))?;
        self.advance_frame(py, frame)
    }

    fn advance_frame(&mut self, py: Python<'_>, frame: AsyncFrame) -> PyResult<AsyncNext> {
        match frame {
            AsyncFrame::Array(frame) => self.advance_array(py, frame),
            AsyncFrame::Object(frame) => self.advance_object(py, frame),
            AsyncFrame::Record(frame) => self.advance_record(py, frame),
            AsyncFrame::Union(frame) => self.advance_union(py, frame),
            AsyncFrame::Intersection(frame) => self.advance_intersection(py, frame),
            _ => Err(PyRuntimeError::new_err(
                "Async frame cannot be advanced without a child result",
            )),
        }
    }
}

fn clone_path(py: Python<'_>, path: &Path) -> Path {
    path.iter().map(|part| part.clone_ref(py)).collect()
}

fn child_path(py: Python<'_>, path: &Path, part: PathPart) -> Path {
    let mut child = clone_path(py, path);
    child.push(part);
    child
}

fn deliver_at(outcome: Result<Py<PyAny>>, path: Path) -> AsyncNext {
    AsyncNext::Task(AsyncTask::Deliver { outcome, path })
}

fn check_awaitable(py: Python<'_>, result: &Bound<'_, PyAny>) -> Result<Py<PyAny>> {
    let inspect = py.import("inspect")?;
    if inspect.call_method1("isasyncgen", (result,))?.is_truthy()? {
        return Err(
            PyTypeError::new_err("Async callbacks must not return async generators").into(),
        );
    }
    if !inspect
        .call_method1("isawaitable", (result,))?
        .is_truthy()?
    {
        return Err(PyTypeError::new_err("Async callback must return an awaitable").into());
    }
    Ok(result.clone().unbind())
}

fn visit_async_frame(
    frame: &AsyncFrame,
    visit: &PyVisit<'_>,
) -> std::result::Result<(), PyTraverseError> {
    match frame {
        AsyncFrame::AsyncCallback(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::Catch(frame) => {
            visit.call(&frame.node)?;
            visit.call(&frame.fallback)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::Refine(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::SuperRefine(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::PostChild(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::StringBool(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::Pipe(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
        }
        AsyncFrame::Array(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
            for item in &frame.items {
                visit.call(item)?;
            }
            for item in &frame.result {
                visit.call(item)?;
            }
            for issue in &frame.issues {
                visit_issue(issue, visit)?;
            }
            if let Some(rest) = &frame.rest {
                visit.call(rest)?;
            }
        }
        AsyncFrame::Object(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
            for key in &frame.keys {
                visit.call(key)?;
            }
            for value in frame.values.iter().flatten() {
                visit.call(value)?;
            }
            visit.call(&frame.result)?;
            for issue in &frame.issues {
                visit_issue(issue, visit)?;
            }
            for (key, value) in &frame.extras {
                visit.call(key)?;
                visit.call(value)?;
            }
        }
        AsyncFrame::Record(frame) => {
            visit.call(&frame.node)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
            for (key, value) in &frame.entries {
                visit.call(key)?;
                visit.call(value)?;
            }
            if let Some(key) = &frame.parsed_key {
                visit.call(key)?;
            }
            visit.call(&frame.result)?;
            for issue in &frame.issues {
                visit_issue(issue, visit)?;
            }
            if let Some(key) = &frame.key_node {
                visit.call(key)?;
            }
        }
        AsyncFrame::Union(frame) => {
            visit.call(&frame.node)?;
            visit.call(&frame.value)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
            for branch in &frame.branches {
                for issue in branch {
                    visit_issue(issue, visit)?;
                }
            }
        }
        AsyncFrame::Intersection(frame) => {
            visit.call(&frame.node)?;
            visit.call(&frame.value)?;
            for part in &frame.path {
                part.visit(visit)?;
            }
            if let Some(outcome) = &frame.left {
                match outcome {
                    StoredOutcome::Value(value) => visit.call(value)?,
                    StoredOutcome::Invalid(issues) => {
                        for issue in issues {
                            visit_issue(issue, visit)?;
                        }
                    }
                    StoredOutcome::Python(error) => {
                        visit.call(&error.value)?;
                        if let Some(traceback) = &error.traceback {
                            visit.call(traceback)?;
                        }
                    }
                }
            }
        }
    }
    Ok(())
}
impl AsyncSession {
    #[allow(clippy::too_many_arguments)]
    fn start_object(
        &mut self,
        py: Python<'_>,
        ctx: &Context<'_>,
        node_py: Py<NativeSchema>,
        node: &NativeSchema,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        if value.bind(py).is(&ctx.missing) {
            let outcome = ctx.fail("required", "Required", &path);
            return Ok(deliver_at(outcome, path));
        }
        let Some(dict) = value.bind(py).cast::<PyDict>().ok() else {
            let params = PyDict::new(py);
            params.set_item("expected", "dict with string keys")?;
            let outcome = ctx.fail_with(
                "invalid_type",
                "Expected dict with string keys",
                &path,
                params,
            );
            return Ok(deliver_at(outcome, path));
        };
        let build = (|| -> PyResult<ObjectFrame> {
            let keys = node.args[0].bind(py).cast::<PyTuple>()?;
            let result = PyDict::new(py);
            let mut values = Vec::with_capacity(keys.len());
            let mut fast = Vec::with_capacity(keys.len());
            let mut present = 0;
            for (index, key) in keys.iter().enumerate() {
                let found = dict.get_item(&key)?;
                if let Some(raw) = found {
                    present += 1;
                    let child = node.children[index].try_borrow(py)?;
                    let is_fast = depth + 1 < MAX_DEPTH
                        && is_primitive_kind(child.kind)
                        && primitive_matches(&raw, child.kind)?;
                    values.push(Some(raw.unbind()));
                    fast.push(is_fast);
                } else {
                    values.push(Some(ctx.missing.clone().unbind()));
                    fast.push(false);
                }
            }
            let extras = if present == dict.len() {
                Vec::new()
            } else {
                let snapshot = dict.copy()?;
                snapshot
                    .iter()
                    .map(|(key, value)| (key.unbind(), value.unbind()))
                    .collect()
            };
            Ok(ObjectFrame {
                node: node_py,
                depth,
                keys: keys.iter().map(Bound::unbind).collect(),
                values,
                fast,
                cursor: 0,
                result: result.unbind(),
                issues: Vec::new(),
                extras,
                extra_cursor: 0,
                extras_phase: false,
                path: clone_path(py, &path),
            })
        })();
        match build {
            Ok(frame) => {
                self.frames.push(AsyncFrame::Object(frame));
                self.advance(py)
            }
            Err(error) => Ok(deliver_at(Err(Error::Python(error)), path)),
        }
    }

    fn advance_object(&mut self, py: Python<'_>, mut frame: ObjectFrame) -> PyResult<AsyncNext> {
        let policy = frame
            .node
            .bind(py)
            .try_borrow()
            .map_err(PyErr::from)?
            .unknown_keys;
        let catchall = frame.node.bind(py).try_borrow().map_err(PyErr::from)?.args[2].clone_ref(py);
        loop {
            if !frame.extras_phase {
                while frame.cursor < frame.values.len() && frame.fast[frame.cursor] {
                    let index = frame.cursor;
                    let raw = frame.values[index].take().expect("fast object field");
                    if let Err(error) = frame
                        .result
                        .bind(py)
                        .set_item(frame.keys[index].bind(py), raw)
                    {
                        return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                    }
                    frame.cursor += 1;
                }
                if frame.cursor < frame.values.len() {
                    let index = frame.cursor;
                    let key = frame.keys[index].clone_ref(py);
                    let raw = frame.values[index].take().expect("non-fast object field");
                    frame.cursor += 1;
                    let child = frame
                        .node
                        .bind(py)
                        .try_borrow()
                        .map_err(PyErr::from)?
                        .children[index]
                        .clone_ref(py);
                    let child_path = child_path(py, &frame.path, PathPart::Value(key));
                    let depth = frame.depth;
                    self.frames.push(AsyncFrame::Object(frame));
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value: raw,
                        path: child_path,
                        depth: depth + 1,
                    }));
                }
                frame.extras_phase = true;
            }
            if frame.extra_cursor >= frame.extras.len() {
                let outcome = if frame.issues.is_empty() {
                    Ok(frame.result.clone_ref(py).into_any())
                } else {
                    Err(Error::Invalid(frame.issues))
                };
                return Ok(deliver_at(outcome, frame.path));
            }
            let index = frame.extra_cursor;
            frame.extra_cursor += 1;
            let (key, raw) = &frame.extras[index];
            let key = key.clone_ref(py);
            let raw = raw.clone_ref(py);
            let key_bound = key.bind(py);
            if !key_bound.is_instance_of::<PyString>() {
                let params = PyDict::new(py);
                params.set_item("expected", "string")?;
                frame.issues.push(Issue::new(
                    py,
                    "invalid_key",
                    "Object keys must be strings",
                    &frame.path,
                    params.into_any(),
                ));
                continue;
            }
            let mut known = false;
            for candidate in &frame.keys {
                if key_bound.eq(candidate.bind(py))? {
                    known = true;
                    break;
                }
            }
            if known {
                continue;
            }
            match policy {
                UnknownKeys::Strict => {
                    let params = PyDict::new(py);
                    params.set_item("keys", PyTuple::new(py, vec![key.clone_ref(py)])?)?;
                    let issue_path =
                        child_path(py, &frame.path, PathPart::Value(key.clone_ref(py)));
                    frame.issues.push(Issue::new(
                        py,
                        "unrecognized_key",
                        "Unrecognized key",
                        &issue_path,
                        params.into_any(),
                    ));
                }
                UnknownKeys::Passthrough => {
                    let value = match deepcopy(raw.bind(py)) {
                        Ok(value) => value,
                        Err(error) => {
                            return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                        }
                    };
                    if let Err(error) = frame.result.bind(py).set_item(&key, value) {
                        return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                    }
                }
                UnknownKeys::Catchall => {
                    let catchall_node = match catchall.bind(py).extract::<Py<NativeSchema>>() {
                        Ok(node) => node,
                        Err(error) => {
                            return Ok(deliver_at(Err(Error::Python(error.into())), frame.path));
                        }
                    };
                    let child_path =
                        child_path(py, &frame.path, PathPart::Value(key.clone_ref(py)));
                    let depth = frame.depth;
                    self.frames.push(AsyncFrame::Object(frame));
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: catchall_node,
                        value: raw,
                        path: child_path,
                        depth: depth + 1,
                    }));
                }
                UnknownKeys::Strip => {}
            }
        }
    }

    fn resume_object(
        &mut self,
        py: Python<'_>,
        mut frame: ObjectFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let extra_child = frame.extras_phase;
        let index = if extra_child {
            frame.extra_cursor.saturating_sub(1)
        } else {
            frame.cursor.saturating_sub(1)
        };
        match outcome {
            Ok(value) => {
                let missing = self
                    .missing
                    .as_ref()
                    .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
                    .bind(py);
                let should_set = extra_child || !value.bind(py).is(missing);
                if should_set {
                    let key = if extra_child {
                        frame.extras[index].0.clone_ref(py)
                    } else {
                        frame.keys[index].clone_ref(py)
                    };
                    if let Err(error) = frame.result.bind(py).set_item(key.bind(py), value) {
                        return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                    }
                }
            }
            Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        }
        self.frames.push(AsyncFrame::Object(frame));
        self.advance(py)
    }

    #[allow(clippy::too_many_arguments)]
    fn start_array(
        &mut self,
        py: Python<'_>,
        ctx: &Context<'_>,
        node_py: Py<NativeSchema>,
        node: &NativeSchema,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        let is_tuple = matches!(node.kind, Kind::Tuple);
        let bound = value.bind(py);
        if bound.is(&ctx.missing) {
            let outcome = ctx.fail("required", "Required", &path);
            return Ok(deliver_at(outcome, path));
        }
        if !bound.is_instance_of::<PyList>() && !(is_tuple && bound.is_instance_of::<PyTuple>()) {
            let expected = if is_tuple { "tuple or list" } else { "list" };
            let params = PyDict::new(py);
            params.set_item("expected", expected)?;
            let outcome = ctx.fail_with(
                "invalid_type",
                &format!("Expected {expected}"),
                &path,
                params,
            );
            return Ok(deliver_at(outcome, path));
        }
        let items = (|| -> PyResult<Vec<Py<PyAny>>> {
            let mut items = Vec::new();
            if bound.is_exact_instance_of::<PyList>() {
                for item in bound.cast::<PyList>()?.iter() {
                    items.push(item.unbind());
                }
            } else if is_tuple && bound.is_exact_instance_of::<PyTuple>() {
                for item in bound.cast::<PyTuple>()?.iter() {
                    items.push(item.unbind());
                }
            } else {
                for item in bound.try_iter()? {
                    items.push(item?.unbind());
                }
            }
            Ok(items)
        })();
        let items = match items {
            Ok(items) => items,
            Err(error) => return Ok(deliver_at(Err(Error::Python(error)), path)),
        };
        let fixed = node.children.len();
        let rest = if is_tuple {
            let rest = node.args[0].bind(py);
            if rest.is_none() {
                if items.len() != fixed {
                    let params = PyDict::new(py);
                    params.set_item("expected", fixed)?;
                    let outcome = ctx.fail_with(
                        "invalid_length",
                        &format!("Expected {fixed} items"),
                        &path,
                        params,
                    );
                    return Ok(deliver_at(outcome, path));
                }
                None
            } else {
                if items.len() < fixed {
                    let params = PyDict::new(py);
                    params.set_item("minimum", fixed)?;
                    params.set_item("inclusive", true)?;
                    params.set_item("origin", "array")?;
                    let outcome = ctx.fail_with(
                        "too_small",
                        &format!("Expected at least {fixed} items"),
                        &path,
                        params,
                    );
                    return Ok(deliver_at(outcome, path));
                }
                match rest.extract::<Py<NativeSchema>>() {
                    Ok(rest) => Some(rest),
                    Err(error) => {
                        return Ok(deliver_at(Err(Error::Python(error.into())), path));
                    }
                }
            }
        } else {
            None
        };
        self.frames.push(AsyncFrame::Array(ArrayFrame {
            node: node_py,
            path,
            depth,
            items,
            index: 0,
            result: Vec::new(),
            issues: Vec::new(),
            is_tuple,
            fixed,
            rest,
        }));
        self.advance(py)
    }

    fn advance_array(&mut self, py: Python<'_>, mut frame: ArrayFrame) -> PyResult<AsyncNext> {
        if frame.index >= frame.items.len() {
            if !frame.issues.is_empty() {
                return Ok(deliver_at(Err(Error::Invalid(frame.issues)), frame.path));
            }
            let outcome = if frame.is_tuple {
                Ok(PyTuple::new(py, frame.result)?.into_any().unbind())
            } else {
                Ok(PyList::new(py, frame.result)?.into_any().unbind())
            };
            return Ok(deliver_at(outcome, frame.path));
        }
        let index = frame.index;
        frame.index += 1;
        let value = frame.items[index].clone_ref(py);
        let child = if frame.is_tuple && index >= frame.fixed {
            frame
                .rest
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("Async tuple has no rest schema"))?
                .clone_ref(py)
        } else {
            frame
                .node
                .bind(py)
                .try_borrow()
                .map_err(PyErr::from)?
                .children[if frame.is_tuple { index } else { 0 }]
            .clone_ref(py)
        };
        let path = child_path(py, &frame.path, PathPart::Index(index));
        let depth = frame.depth;
        self.frames.push(AsyncFrame::Array(frame));
        Ok(AsyncNext::Task(AsyncTask::Eval {
            node: child,
            value,
            path,
            depth: depth + 1,
        }))
    }

    fn resume_array(
        &mut self,
        py: Python<'_>,
        mut frame: ArrayFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match outcome {
            Ok(value) => frame.result.push(value),
            Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        }
        self.frames.push(AsyncFrame::Array(frame));
        self.advance(py)
    }

    #[allow(clippy::too_many_arguments)]
    fn start_record(
        &mut self,
        py: Python<'_>,
        ctx: &Context<'_>,
        node_py: Py<NativeSchema>,
        node: &NativeSchema,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        if value.bind(py).is(&ctx.missing) {
            let outcome = ctx.fail("required", "Required", &path);
            return Ok(deliver_at(outcome, path));
        }
        let Some(dict) = value.bind(py).cast::<PyDict>().ok() else {
            let params = PyDict::new(py);
            params.set_item("expected", "dict")?;
            let outcome = ctx.fail_with("invalid_type", "Expected dict", &path, params);
            return Ok(deliver_at(outcome, path));
        };
        let build = (|| -> PyResult<RecordFrame> {
            let snapshot = dict.copy()?;
            let entries = snapshot
                .iter()
                .map(|(key, value)| Ok((key.unbind(), value.unbind())))
                .collect::<PyResult<Vec<_>>>()?;
            let key_schema = node.args[0].bind(py);
            let key_node = if key_schema.is_none() {
                None
            } else {
                Some(key_schema.extract::<Py<NativeSchema>>()?)
            };
            Ok(RecordFrame {
                node: node_py,
                path: clone_path(py, &path),
                depth,
                entries,
                index: 0,
                stage: RecordStage::Key,
                parsed_key: None,
                result: PyDict::new(py).unbind(),
                issues: Vec::new(),
                key_node,
            })
        })();
        match build {
            Ok(frame) => {
                self.frames.push(AsyncFrame::Record(frame));
                self.advance(py)
            }
            Err(error) => Ok(deliver_at(Err(Error::Python(error)), path)),
        }
    }

    fn advance_record(&mut self, py: Python<'_>, mut frame: RecordFrame) -> PyResult<AsyncNext> {
        loop {
            match frame.stage {
                RecordStage::Key => {
                    if frame.index >= frame.entries.len() {
                        let outcome = if frame.issues.is_empty() {
                            Ok(frame.result.clone_ref(py).into_any())
                        } else {
                            Err(Error::Invalid(frame.issues))
                        };
                        return Ok(deliver_at(outcome, frame.path));
                    }
                    let key = frame.entries[frame.index].0.clone_ref(py);
                    if let Some(key_node) = &frame.key_node {
                        let key_node = key_node.clone_ref(py);
                        frame.stage = RecordStage::KeyWaiting;
                        let depth = frame.depth;
                        let path = clone_path(py, &frame.path);
                        self.frames.push(AsyncFrame::Record(frame));
                        return Ok(AsyncNext::Task(AsyncTask::Eval {
                            node: key_node,
                            value: key,
                            path,
                            depth: depth + 1,
                        }));
                    }
                    if !key.bind(py).is_instance_of::<PyString>() {
                        let params = PyDict::new(py);
                        params.set_item("expected", "string")?;
                        frame.issues.push(Issue::new(
                            py,
                            "invalid_key",
                            "Record keys must be strings",
                            &frame.path,
                            params.into_any(),
                        ));
                        frame.index += 1;
                        continue;
                    }
                    frame.parsed_key = Some(key);
                    frame.stage = RecordStage::Value;
                }
                RecordStage::Value => {
                    let value = frame.entries[frame.index].1.clone_ref(py);
                    let parsed_key = frame
                        .parsed_key
                        .as_ref()
                        .ok_or_else(|| PyRuntimeError::new_err("Record has no parsed key"))?
                        .clone_ref(py);
                    frame.stage = RecordStage::ValueWaiting;
                    let depth = frame.depth;
                    let path = child_path(py, &frame.path, PathPart::Value(parsed_key));
                    let child = frame
                        .node
                        .bind(py)
                        .try_borrow()
                        .map_err(PyErr::from)?
                        .children[0]
                        .clone_ref(py);
                    self.frames.push(AsyncFrame::Record(frame));
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value,
                        path,
                        depth: depth + 1,
                    }));
                }
                RecordStage::KeyWaiting | RecordStage::ValueWaiting => {
                    return Err(PyRuntimeError::new_err(
                        "Record frame cannot advance while awaiting",
                    ));
                }
            }
        }
    }

    fn resume_record(
        &mut self,
        py: Python<'_>,
        mut frame: RecordFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match frame.stage {
            RecordStage::KeyWaiting => {
                frame.stage = RecordStage::Key;
                match outcome {
                    Ok(parsed_key) => {
                        frame.parsed_key = Some(parsed_key);
                        frame.stage = RecordStage::Value;
                    }
                    Err(Error::Invalid(mut issues)) => {
                        frame.issues.append(&mut issues);
                        frame.index += 1;
                        frame.parsed_key = None;
                    }
                    Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                }
            }
            RecordStage::ValueWaiting => {
                frame.stage = RecordStage::Key;
                let parsed_key = frame
                    .parsed_key
                    .take()
                    .ok_or_else(|| PyRuntimeError::new_err("Record has no parsed key"))?;
                match outcome {
                    Ok(value) => {
                        if let Err(error) =
                            frame.result.bind(py).set_item(parsed_key.bind(py), value)
                        {
                            return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                        }
                        frame.index += 1;
                    }
                    Err(Error::Invalid(mut issues)) => {
                        frame.issues.append(&mut issues);
                        frame.index += 1;
                    }
                    Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                }
            }
            RecordStage::Key | RecordStage::Value => {
                return Err(PyRuntimeError::new_err(
                    "Record frame received an unexpected result",
                ));
            }
        }
        self.frames.push(AsyncFrame::Record(frame));
        self.advance(py)
    }

    fn start_union(
        &mut self,
        py: Python<'_>,
        node_py: Py<NativeSchema>,
        _node: &NativeSchema,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        self.frames.push(AsyncFrame::Union(UnionFrame {
            node: node_py,
            value,
            path,
            depth,
            index: 0,
            branches: Vec::new(),
        }));
        self.advance(py)
    }

    fn advance_union(&mut self, py: Python<'_>, mut frame: UnionFrame) -> PyResult<AsyncNext> {
        let children = frame
            .node
            .bind(py)
            .try_borrow()
            .map_err(PyErr::from)?
            .children
            .len();
        if frame.index < children {
            let index = frame.index;
            frame.index += 1;
            let child = frame
                .node
                .bind(py)
                .try_borrow()
                .map_err(PyErr::from)?
                .children[index]
                .clone_ref(py);
            let value = frame.value.clone_ref(py);
            let path = clone_path(py, &frame.path);
            let depth = frame.depth;
            self.frames.push(AsyncFrame::Union(frame));
            return Ok(AsyncNext::Task(AsyncTask::Eval {
                node: child,
                value,
                path,
                depth: depth + 1,
            }));
        }
        let mut issue = Issue::simple(py, "invalid_union", "No union branch matched", &frame.path);
        issue.branches = frame.branches;
        Ok(deliver_at(Err(Error::Invalid(vec![issue])), frame.path))
    }

    fn resume_union(
        &mut self,
        py: Python<'_>,
        mut frame: UnionFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match outcome {
            Ok(value) => Ok(deliver_at(Ok(value), frame.path)),
            Err(Error::Invalid(issues)) => {
                frame.branches.push(issues);
                self.frames.push(AsyncFrame::Union(frame));
                self.advance(py)
            }
            Err(error) => Ok(deliver_at(Err(error), frame.path)),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn start_intersection(
        &mut self,
        py: Python<'_>,
        ctx: &Context<'_>,
        node_py: Py<NativeSchema>,
        _node: &NativeSchema,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        if value.bind(py).is(&ctx.missing) {
            let outcome = ctx.fail("required", "Required", &path);
            return Ok(deliver_at(outcome, path));
        }
        self.frames
            .push(AsyncFrame::Intersection(IntersectionFrame {
                node: node_py,
                value,
                path,
                depth,
                stage: 0,
                left: None,
            }));
        self.advance(py)
    }

    fn advance_intersection(
        &mut self,
        py: Python<'_>,
        mut frame: IntersectionFrame,
    ) -> PyResult<AsyncNext> {
        let index = match frame.stage {
            0 => 0,
            2 => 1,
            _ => {
                return Err(PyRuntimeError::new_err(
                    "Intersection frame cannot advance in this stage",
                ));
            }
        };
        frame.stage += 1;
        let child = frame
            .node
            .bind(py)
            .try_borrow()
            .map_err(PyErr::from)?
            .children[index]
            .clone_ref(py);
        let value = frame.value.clone_ref(py);
        let path = clone_path(py, &frame.path);
        let depth = frame.depth;
        self.frames.push(AsyncFrame::Intersection(frame));
        Ok(AsyncNext::Task(AsyncTask::Eval {
            node: child,
            value,
            path,
            depth: depth + 1,
        }))
    }

    fn resume_intersection(
        &mut self,
        py: Python<'_>,
        mut frame: IntersectionFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match frame.stage {
            1 => {
                frame.left = Some(StoredOutcome::store(py, outcome));
                frame.stage = 2;
                self.frames.push(AsyncFrame::Intersection(frame));
                self.advance(py)
            }
            3 => {
                let left = frame
                    .left
                    .take()
                    .ok_or_else(|| PyRuntimeError::new_err("Intersection has no left result"))?
                    .restore(py);
                self.combine_intersection(py, frame.path, left, outcome)
            }
            _ => Err(PyRuntimeError::new_err(
                "Intersection frame received an unexpected result",
            )),
        }
    }

    fn combine_intersection(
        &self,
        py: Python<'_>,
        path: Path,
        left: Result<Py<PyAny>>,
        right: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match (left, right) {
            (Ok(left), Ok(right)) => {
                let left = left.bind(py);
                let right = right.bind(py);
                if !left.is_instance_of::<PyDict>() || !right.is_instance_of::<PyDict>() {
                    let ctx = self.context(py)?;
                    let outcome = ctx.fail(
                        "invalid_intersection_types",
                        "Intersection outputs must be dictionaries",
                        &path,
                    );
                    return Ok(deliver_at(outcome, path));
                }
                let merged = merge_dicts(
                    left.cast::<PyDict>().map_err(PyErr::from)?,
                    right.cast::<PyDict>().map_err(PyErr::from)?,
                    0,
                );
                Ok(deliver_at(merged.map_err(Error::Python), path))
            }
            (Err(Error::Invalid(mut left_issues)), Err(Error::Invalid(mut right_issues))) => {
                left_issues.append(&mut right_issues);
                Ok(deliver_at(Err(Error::Invalid(left_issues)), path))
            }
            (Err(Error::Invalid(issues)), Ok(_)) | (Ok(_), Err(Error::Invalid(issues))) => {
                Ok(deliver_at(Err(Error::Invalid(issues)), path))
            }
            (Err(Error::Python(error)), _) | (_, Err(Error::Python(error))) => {
                Ok(deliver_at(Err(Error::Python(error)), path))
            }
        }
    }

    fn resume_async_callback(
        &mut self,
        py: Python<'_>,
        frame: AsyncCallbackFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let parsed = match outcome {
            Ok(parsed) => parsed,
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        };
        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
        let callback = node.args[0].clone_ref(py);
        let pending_kind = match node.kind {
            Kind::AsyncRefine => AsyncPendingKind::Refine,
            Kind::AsyncSuperRefine => AsyncPendingKind::SuperRefine,
            Kind::AsyncTransform => AsyncPendingKind::Transform,
            Kind::AsyncOverwrite => AsyncPendingKind::Overwrite,
            _ => {
                drop(node);
                return Ok(deliver_at(
                    Err(
                        PyTypeError::new_err("Async session is not awaiting an async callback")
                            .into(),
                    ),
                    frame.path,
                ));
            }
        };
        drop(node);
        let result = match callback.bind(py).call1((parsed.bind(py),)) {
            Ok(result) => result,
            Err(error) => {
                return Ok(deliver_at(Err(Error::Python(error)), frame.path));
            }
        };
        let awaitable = match check_awaitable(py, &result) {
            Ok(awaitable) => awaitable,
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        };
        self.token = ASYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
        self.pending = Some(AsyncPending {
            node: frame.node,
            kind: pending_kind,
            parsed: Some(parsed),
            child: None,
            path: frame.path,
            depth: 0,
        });
        self.awaitable = Some(awaitable);
        Ok(AsyncNext::Suspend)
    }

    fn resume_callback(
        &mut self,
        py: Python<'_>,
        pending: AsyncPending,
        value: Py<PyAny>,
    ) -> PyResult<AsyncNext> {
        let AsyncPending {
            node,
            kind,
            parsed,
            child,
            path,
            depth,
        } = pending;
        match kind {
            AsyncPendingKind::Refine => {
                let parsed =
                    parsed.ok_or_else(|| PyRuntimeError::new_err("Async session has no result"))?;
                let truthy = match value.bind(py).is_truthy() {
                    Ok(truthy) => truthy,
                    Err(error) => {
                        return Ok(deliver_at(Err(Error::Python(error)), path));
                    }
                };
                if truthy {
                    return Ok(deliver_at(Ok(parsed), path));
                }
                let node = node.bind(py).try_borrow().map_err(PyErr::from)?;
                let message: String = node.args[1].bind(py).extract()?;
                let relative = node.args[2].bind(py);
                let mut issue_path = clone_path(py, &path);
                for part in relative.try_iter()? {
                    issue_path.push(PathPart::Value(part?.unbind()));
                }
                let params = PyDict::new(py);
                let issue = Issue::new(py, "custom", &message, &issue_path, params.into_any());
                Ok(deliver_at(Err(Error::Invalid(vec![issue])), path))
            }
            AsyncPendingKind::SuperRefine => {
                let parsed =
                    parsed.ok_or_else(|| PyRuntimeError::new_err("Async session has no result"))?;
                let ctx = self.context(py)?;
                let value = value.bind(py);
                let mut issues = Vec::new();
                if value.is_none() {
                    return Ok(deliver_at(Ok(parsed), path));
                }
                if value.is_instance(&ctx.issue_type)? {
                    issues.push(Issue::from_python(value)?);
                } else if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
                    for item in value.try_iter()? {
                        let item = item?;
                        if !item.is_instance(&ctx.issue_type)? {
                            return Ok(deliver_at(
                                Err(PyTypeError::new_err(
                                    "super_refine_async callback returned a non-Issue value",
                                )
                                .into()),
                                path,
                            ));
                        }
                        issues.push(Issue::from_python(&item)?);
                    }
                } else {
                    return Ok(deliver_at(
                        Err(PyTypeError::new_err(
                            "super_refine_async callback must return Issue, a list or tuple of Issue, or None",
                        )
                        .into()),
                        path,
                    ));
                }
                if issues.is_empty() {
                    return Ok(deliver_at(Ok(parsed), path));
                }
                for issue in &mut issues {
                    let mut absolute = clone_path(py, &path);
                    absolute.extend(issue.path.iter().map(|part| part.clone_ref(py)));
                    issue.path = absolute;
                }
                Ok(deliver_at(Err(Error::Invalid(issues)), path))
            }
            AsyncPendingKind::Transform
            | AsyncPendingKind::Overwrite
            | AsyncPendingKind::CatchFallback => Ok(deliver_at(Ok(value), path)),
            AsyncPendingKind::Preprocess | AsyncPendingKind::DefaultFactory => {
                let child = child
                    .ok_or_else(|| PyRuntimeError::new_err("Async session has no child schema"))?;
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
        }
    }
}

struct Context<'py> {
    py: Python<'py>,
    missing: Bound<'py, PyAny>,
    issue_type: Bound<'py, PyAny>,
    error_type: Bound<'py, PyAny>,
    lazy_store: LazyHandle,
}

impl<'py> Context<'py> {
    fn new(
        py: Python<'py>,
        missing: Bound<'py, PyAny>,
        issue_type: Bound<'py, PyAny>,
        error_type: Bound<'py, PyAny>,
    ) -> Self {
        Self {
            py,
            missing,
            issue_type,
            error_type,
            lazy_store: LazyHandle::Local(RefCell::new(LazyState::default())),
        }
    }
}

impl Context<'_> {
    fn fail<T>(&self, code: &str, message: &str, path: &Path) -> Result<T> {
        self.fail_with(code, message, path, PyDict::new(self.py))
    }

    fn fail_template<T>(&self, template: &ErrorTemplate, path: &Path) -> Result<T> {
        Err(Error::Invalid(vec![Issue::from_template(
            self.py, template, path,
        )]))
    }

    fn fail_with<T>(
        &self,
        code: &str,
        message: &str,
        path: &Path,
        params: Bound<'_, PyDict>,
    ) -> Result<T> {
        Err(Error::Invalid(vec![Issue::new(
            self.py,
            code,
            message,
            path,
            params.into_any(),
        )]))
    }

    // A Python callback can deliberately raise our public ValidationError.
    // Normalize it so its issues also aggregate with sibling failures.
    fn normalize(&self, error: Error, path: &Path) -> Error {
        if let Error::Python(error) = error {
            if error
                .matches(self.py, &self.error_type)
                .is_ok_and(|matched| matched)
            {
                let converted = (|| {
                    error
                        .value(self.py)
                        .getattr("issues")?
                        .try_iter()?
                        .map(|issue| Issue::from_python(&issue?))
                        .collect::<PyResult<Vec<_>>>()
                })();
                return match converted {
                    // A rejection with no details must still fail and aggregate.
                    Ok(issues) if issues.is_empty() => Error::Invalid(vec![Issue::simple(
                        self.py,
                        "custom",
                        "Invalid value",
                        path,
                    )]),
                    Ok(issues) => Error::Invalid(issues),
                    Err(error) => Error::Python(error),
                };
            }
            Error::Python(error)
        } else {
            error
        }
    }

    fn make_exception(&self, error: Error) -> PyErr {
        match error {
            Error::Python(error) => error,
            Error::Invalid(issues) => {
                let exception = (move || {
                    let bundle = Py::new(self.py, NativeIssueBundle { issues })?;
                    self.error_type.call_method1("_from_bundle", (bundle,))
                })();
                match exception {
                    Ok(value) => PyErr::from_value(value),
                    Err(error) => error,
                }
            }
        }
    }
}

#[derive(Clone, Copy)]
enum Kind {
    String,
    Number,
    Integer,
    Boolean,
    Unknown,
    None,
    ExactInstance,
    Json,
    NativeEnum,
    Literal,
    Enum,
    Instance,
    Array,
    Object,
    Union,
    DiscriminatedUnion,
    Intersection,
    Record,
    Tuple,
    Lazy,
    Optional,
    NonOptional,
    Nullable,
    Default,
    DefaultFactory,
    Refine,
    SuperRefine,
    Transform,
    AsyncRefine,
    AsyncSuperRefine,
    AsyncTransform,
    AsyncPreprocess,
    AsyncDefaultFactory,
    AsyncCatch,
    AsyncOverwrite,
    Overwrite,
    Catch,
    Coerce,
    StringBool,
    Preprocess,
    Pipe,
    LengthMin,
    LengthMax,
    Min,
    Max,
    ExclusiveMin,
    ExclusiveMax,
    MultipleOf,
    Positive,
    Integral,
    Regex,
    StartsWith,
    EndsWith,
    Includes,
    Ipv6,
    Trim,
    Lower,
    Upper,
    Cleared,
}

impl Kind {
    fn name(&self) -> &'static str {
        match self {
            Self::String => "string",
            Self::Number => "number",
            Self::Integer => "integer",
            Self::Boolean => "boolean",
            Self::Unknown => "unknown",
            Self::None => "none",
            Self::ExactInstance => "exact_instance",
            Self::Json => "json",
            Self::NativeEnum => "native_enum",
            Self::Literal => "literal",
            Self::Enum => "enum",
            Self::Instance => "instance",
            Self::Array => "array",
            Self::Object => "object",
            Self::Union => "union",
            Self::DiscriminatedUnion => "discriminated_union",
            Self::Intersection => "intersection",
            Self::Record => "record",
            Self::Tuple => "tuple",
            Self::Lazy => "lazy",
            Self::Optional => "optional",
            Self::NonOptional => "nonoptional",
            Self::Nullable => "nullable",
            Self::Default => "default",
            Self::DefaultFactory => "default_factory",
            Self::Refine => "refine",
            Self::SuperRefine => "super_refine",
            Self::Transform => "transform",
            Self::AsyncRefine => "async_refine",
            Self::AsyncSuperRefine => "async_super_refine",
            Self::AsyncTransform => "async_transform",
            Self::AsyncPreprocess => "async_preprocess",
            Self::AsyncDefaultFactory => "async_default_factory",
            Self::AsyncCatch => "async_catch",
            Self::AsyncOverwrite => "async_overwrite",
            Self::Overwrite => "overwrite",
            Self::Catch => "catch",
            Self::Coerce => "coerce",
            Self::StringBool => "string_bool",
            Self::Preprocess => "preprocess",
            Self::Pipe => "pipe",
            Self::LengthMin => "length_min",
            Self::LengthMax => "length_max",
            Self::Min => "min",
            Self::Max => "max",
            Self::ExclusiveMin => "exclusive_min",
            Self::ExclusiveMax => "exclusive_max",
            Self::MultipleOf => "multiple_of",
            Self::Positive => "positive",
            Self::Integral => "integral",
            Self::Regex => "regex",
            Self::StartsWith => "starts_with",
            Self::EndsWith => "ends_with",
            Self::Includes => "includes",
            Self::Ipv6 => "ipv6",
            Self::Trim => "trim",
            Self::Lower => "lower",
            Self::Upper => "upper",
            Self::Cleared => "cleared",
        }
    }

    fn from_name(name: &str, children: usize, args: usize) -> PyResult<Self> {
        // Validate arity before any node is accepted; evaluation never indexes
        // unchecked user-supplied vectors.
        let (kind, expected_children, expected_args) = match name {
            "string" => (Self::String, Some(0), 0),
            "number" => (Self::Number, Some(0), 0),
            "integer" => (Self::Integer, Some(0), 0),
            "boolean" => (Self::Boolean, Some(0), 0),
            "unknown" => (Self::Unknown, Some(0), 0),
            "none" => (Self::None, Some(0), 0),
            "exact_instance" => (Self::ExactInstance, Some(0), 1),
            "json" => (Self::Json, Some(0), 0),
            "native_enum" => (Self::NativeEnum, Some(0), 1),
            "literal" => (Self::Literal, Some(0), 1),
            "enum" => (Self::Enum, Some(0), 1),
            "instance" => (Self::Instance, Some(0), 1),
            "array" => (Self::Array, Some(1), 0),
            "object" => (Self::Object, None, 3),
            "union" if children > 0 => (Self::Union, None, 0),
            "discriminated_union" if children > 0 => (Self::DiscriminatedUnion, None, 2),
            "intersection" => (Self::Intersection, Some(2), 0),
            "record" => (Self::Record, Some(1), 1),
            "tuple" => (Self::Tuple, None, 1),
            "lazy" => (Self::Lazy, Some(0), 1),
            "optional" => (Self::Optional, Some(1), 0),
            "nonoptional" => (Self::NonOptional, Some(1), 0),
            "nullable" => (Self::Nullable, Some(1), 0),
            "default" => (Self::Default, Some(1), 1),
            "default_factory" => (Self::DefaultFactory, Some(1), 1),
            "refine" => (Self::Refine, Some(1), 3),
            "super_refine" => (Self::SuperRefine, Some(1), 1),
            "transform" => (Self::Transform, Some(1), 1),
            "async_refine" => (Self::AsyncRefine, Some(1), 3),
            "async_super_refine" => (Self::AsyncSuperRefine, Some(1), 1),
            "async_transform" => (Self::AsyncTransform, Some(1), 1),
            "async_preprocess" => (Self::AsyncPreprocess, Some(1), 1),
            "async_default_factory" => (Self::AsyncDefaultFactory, Some(1), 1),
            "async_catch" => (Self::AsyncCatch, Some(1), 1),
            "async_overwrite" => (Self::AsyncOverwrite, Some(1), 1),
            "overwrite" => (Self::Overwrite, Some(1), 1),
            "catch" => (Self::Catch, Some(1), 1),
            "coerce" => (Self::Coerce, Some(1), 1),
            "string_bool" => (Self::StringBool, Some(1), 4),
            "preprocess" => (Self::Preprocess, Some(1), 1),
            "pipe" => (Self::Pipe, Some(2), 0),
            "length_min" => (Self::LengthMin, Some(1), 2),
            "length_max" => (Self::LengthMax, Some(1), 2),
            "min" => (Self::Min, Some(1), 2),
            "max" => (Self::Max, Some(1), 2),
            "exclusive_min" => (Self::ExclusiveMin, Some(1), 2),
            "exclusive_max" => (Self::ExclusiveMax, Some(1), 2),
            "multiple_of" => (Self::MultipleOf, Some(1), 2),
            "positive" => (Self::Positive, Some(1), 1),
            "integral" => (Self::Integral, Some(1), 1),
            "regex" => (Self::Regex, Some(1), 3),
            "starts_with" => (Self::StartsWith, Some(1), 2),
            "ends_with" => (Self::EndsWith, Some(1), 2),
            "includes" => (Self::Includes, Some(1), 2),
            "ipv6" => (Self::Ipv6, Some(1), 1),
            "trim" => (Self::Trim, Some(1), 0),
            "lower" => (Self::Lower, Some(1), 0),
            "upper" => (Self::Upper, Some(1), 0),
            _ => {
                return Err(PyValueError::new_err(format!(
                    "Invalid native operation: {name}"
                )));
            }
        };
        if expected_children.is_some_and(|count| children != count) || args != expected_args {
            return Err(PyValueError::new_err("Invalid native schema arity"));
        }
        Ok(kind)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum UnknownKeys {
    Strip,
    Strict,
    Passthrough,
    Catchall,
}

#[pyclass(module = "zodish._native")]
struct NativeSchema {
    kind: Kind,
    children: Vec<Py<NativeSchema>>,
    args: Vec<Py<PyAny>>,
    unknown_keys: UnknownKeys,
    json_fast: bool,
    // Conservative capability bit: lazy subtrees are treated as async until
    // their factory resolves. Argument schema edges (object catchall, record
    // key, tuple rest) are included so fast-path selection cannot skip them.
    has_async: bool,
    error_template: Option<ErrorTemplate>,
}

#[pymethods]
impl NativeSchema {
    #[new]
    fn new(
        py: Python<'_>,
        kind: &str,
        children: Vec<Py<Self>>,
        args: Vec<Py<PyAny>>,
    ) -> PyResult<Self> {
        let kind = Kind::from_name(kind, children.len(), args.len())?;
        let mut unknown_keys = UnknownKeys::Strip;
        if matches!(kind, Kind::Object) {
            let keys = args[0].bind(py).cast::<PyTuple>()?;
            let policy: String = args[1].bind(py).extract()?;
            let catchall = args[2].bind(py);
            if keys.len() != children.len()
                || keys.iter().any(|key| !key.is_instance_of::<PyString>())
                || !matches!(
                    policy.as_str(),
                    "strip" | "strict" | "passthrough" | "catchall"
                )
                || (policy == "catchall" && !catchall.is_instance_of::<Self>())
                || (policy != "catchall" && !catchall.is_none())
            {
                return Err(PyValueError::new_err("Invalid native object shape"));
            }
            unknown_keys = match policy.as_str() {
                "strip" => UnknownKeys::Strip,
                "strict" => UnknownKeys::Strict,
                "passthrough" => UnknownKeys::Passthrough,
                "catchall" => UnknownKeys::Catchall,
                _ => unreachable!(),
            };
        }
        if matches!(kind, Kind::Record | Kind::Tuple) {
            let extra = args[0].bind(py);
            if !extra.is_none() && !extra.is_instance_of::<Self>() {
                return Err(PyValueError::new_err("Invalid native container schema"));
            }
        }
        if matches!(kind, Kind::ExactInstance | Kind::NativeEnum)
            && !args[0].bind(py).is_instance_of::<PyType>()
        {
            return Err(PyValueError::new_err("Invalid native type schema"));
        }
        if matches!(kind, Kind::Coerce) {
            let target: String = args[0].bind(py).extract()?;
            if !matches!(target.as_str(), "string" | "number" | "integer" | "boolean") {
                return Err(PyValueError::new_err("Invalid coercion target"));
            }
        }
        if matches!(kind, Kind::StringBool) {
            let truthy = args[0].bind(py).cast::<PyTuple>()?;
            let falsy = args[1].bind(py).cast::<PyTuple>()?;
            if truthy
                .iter()
                .chain(falsy.iter())
                .any(|value| !value.is_instance_of::<PyString>())
                || !args[2].bind(py).is_instance_of::<PyBool>()
            {
                return Err(PyValueError::new_err(
                    "Invalid native stringbool configuration",
                ));
            }
        }
        if matches!(kind, Kind::Enum) {
            let choices = args[0].bind(py).cast::<PyTuple>()?;
            if choices.is_empty() {
                return Err(PyValueError::new_err(
                    "Enum must contain at least one value",
                ));
            }
        }
        if matches!(kind, Kind::DiscriminatedUnion) {
            let key = args[0].bind(py);
            let options = args[1].bind(py).cast::<PyTuple>()?;
            if !key.is_instance_of::<PyString>()
                || options.is_empty()
                || options.len() != children.len()
                || options
                    .iter()
                    .any(|option| !option.is_instance_of::<PyString>())
            {
                return Err(PyValueError::new_err(
                    "Invalid native discriminated union options",
                ));
            }
            let mut seen = HashSet::new();
            for option in options.iter() {
                let option: String = option.extract()?;
                if !seen.insert(option) {
                    return Err(PyValueError::new_err(
                        "Invalid native discriminated union options",
                    ));
                }
            }
        }
        let json_fast = match kind {
            Kind::String | Kind::Integer | Kind::Boolean | Kind::None => true,
            Kind::Literal | Kind::Enum => true,
            Kind::Positive
            | Kind::LengthMin
            | Kind::LengthMax
            | Kind::Min
            | Kind::Max
            | Kind::ExclusiveMin
            | Kind::ExclusiveMax
            | Kind::MultipleOf
            | Kind::Nullable
            | Kind::Optional
            | Kind::NonOptional => children
                .first()
                .map(|child| child.try_borrow(py).map(|child| child.json_fast))
                .transpose()
                .map_err(PyErr::from)?
                .unwrap_or(false),
            Kind::Array => children[0].try_borrow(py).map_err(PyErr::from)?.json_fast,
            Kind::Object => {
                let mut all_fast = true;
                for child in &children {
                    if !child.try_borrow(py).map_err(PyErr::from)?.json_fast {
                        all_fast = false;
                        break;
                    }
                }
                matches!(unknown_keys, UnknownKeys::Strip | UnknownKeys::Strict) && all_fast
            }
            _ => false,
        };
        // Schema edges stored in arguments (object catchall, record key, tuple
        // rest) also determine async capability. Lazy factories are unknown
        // until evaluation, so treat their subtrees conservatively.
        let mut has_async = matches!(
            kind,
            Kind::AsyncRefine
                | Kind::AsyncSuperRefine
                | Kind::AsyncTransform
                | Kind::AsyncPreprocess
                | Kind::AsyncDefaultFactory
                | Kind::AsyncCatch
                | Kind::AsyncOverwrite
                | Kind::Lazy
        ) || children
            .iter()
            .any(|child| child.try_borrow(py).is_ok_and(|child| child.has_async));
        if !has_async {
            for arg in &args {
                let arg = arg.bind(py);
                if arg.is_instance_of::<Self>() {
                    if let Ok(node) = arg.extract::<PyRef<'_, Self>>() {
                        if node.has_async {
                            has_async = true;
                            break;
                        }
                    }
                }
            }
        }
        let error_template = build_error_template(py, kind, &children, &args)
            .ok()
            .flatten();
        Ok(Self {
            kind,
            children,
            args,
            unknown_keys,
            json_fast,
            has_async,
            error_template,
        })
    }

    #[getter]
    fn kind(&self) -> &'static str {
        self.kind.name()
    }

    #[getter]
    fn has_async(&self) -> bool {
        self.has_async
    }

    #[getter]
    fn children(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.children.iter().map(|child| child.clone_ref(py)))?.unbind())
    }

    #[getter]
    fn args(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.args.iter().map(|arg| arg.clone_ref(py)))?.unbind())
    }

    fn parse<'py>(
        &self,
        py: Python<'py>,
        value: Bound<'py, PyAny>,
        missing: Bound<'py, PyAny>,
        issue_type: Bound<'py, PyAny>,
        error_type: Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let ctx = Context::new(py, missing, issue_type, error_type);
        self.validate(&ctx, &value, &mut Vec::new(), 0)
            .map_err(|error| ctx.make_exception(error))
    }

    fn parse_json(&self, py: Python<'_>, data: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let parsed = if self.json_fast {
            match parse_json_value(&data)? {
                Some(value) => self.validate_json_fast(py, &value, 0)?,
                None => None,
            }
        } else {
            None
        };
        match parsed {
            Some(value) => Ok((true, value).into_pyobject(py)?.into_any().unbind()),
            None => Ok((false, py.None()).into_pyobject(py)?.into_any().unbind()),
        }
    }

    // Lazy factories may close over a Python schema which points back to this
    // native node. Participate in Python GC so those cycles are collectible.
    fn __traverse__(&self, visit: PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        for child in &self.children {
            visit.call(child)?;
        }
        for arg in &self.args {
            visit.call(arg)?;
        }
        if let Some(template) = &self.error_template {
            visit.call(&template.params)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.kind = Kind::Cleared;
        self.children.clear();
        self.args.clear();
        self.error_template = None;
    }
}

impl NativeSchema {
    fn validate(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        if depth >= MAX_DEPTH {
            return Err(PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into());
        }
        self.evaluate(ctx, value, path, depth)
            .map_err(|error| ctx.normalize(error, path))
    }

    fn child(
        &self,
        index: usize,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let child = self.children[index]
            .try_borrow(ctx.py)
            .map_err(PyErr::from)?;
        child.validate(ctx, value, path, depth + 1)
    }

    /// Apply a unary post-child operation to an already validated value.
    /// The synchronous evaluator and async continuation frames share this so
    /// constraint behavior cannot drift between parsing modes.
    fn evaluate_post_child(
        &self,
        ctx: &Context<'_>,
        parsed: &Bound<'_, PyAny>,
        path: &Path,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let valid = match self.kind {
            Kind::Transform | Kind::Overwrite => {
                return Ok(self.args[0].bind(py).call1((parsed,))?.unbind());
            }
            Kind::Trim | Kind::Lower | Kind::Upper => {
                let method = match self.kind {
                    Kind::Trim => "strip",
                    Kind::Lower => "lower",
                    _ => "upper",
                };
                return Ok(py
                    .get_type::<PyString>()
                    .call_method1(method, (parsed,))?
                    .unbind());
            }
            Kind::LengthMin | Kind::LengthMax => {
                let length = parsed.len()?.into_pyobject(py)?.into_any();
                if matches!(self.kind, Kind::LengthMin) {
                    length.ge(self.args[0].bind(py))?
                } else {
                    length.le(self.args[0].bind(py))?
                }
            }
            Kind::Min => parsed.ge(self.args[0].bind(py))?,
            Kind::Max => parsed.le(self.args[0].bind(py))?,
            Kind::ExclusiveMin => parsed.gt(self.args[0].bind(py))?,
            Kind::ExclusiveMax => parsed.lt(self.args[0].bind(py))?,
            Kind::MultipleOf => is_multiple_of(parsed, self.args[0].bind(py))?,
            Kind::Positive => parsed.gt(0)?,
            Kind::Integral => {
                parsed.is_exact_instance_of::<PyInt>() || parsed.extract::<f64>()?.fract() == 0.0
            }
            Kind::Regex => !self.args[0]
                .bind(py)
                .call_method1("search", (parsed,))?
                .is_none(),
            Kind::StartsWith => py
                .get_type::<PyString>()
                .call_method1("startswith", (parsed, self.args[0].bind(py)))?
                .is_truthy()?,
            Kind::EndsWith => py
                .get_type::<PyString>()
                .call_method1("endswith", (parsed, self.args[0].bind(py)))?
                .is_truthy()?,
            Kind::Includes => {
                py.get_type::<PyString>()
                    .call_method1("find", (parsed, self.args[0].bind(py)))?
                    .extract::<isize>()?
                    >= 0
            }
            Kind::Ipv6 => parsed
                .extract::<String>()
                .ok()
                .is_some_and(|text| text.parse::<Ipv6Addr>().is_ok()),
            _ => unreachable!(),
        };
        if valid {
            return Ok(parsed.clone().unbind());
        }
        if let Some(template) = &self.error_template {
            return ctx.fail_template(template, path);
        }
        let (code, message, params) = match self.kind {
            Kind::LengthMin => {
                let params = PyDict::new(py);
                params.set_item("minimum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item(
                    "origin",
                    if parsed.is_instance_of::<PyString>() {
                        "string"
                    } else {
                        "array"
                    },
                )?;
                (
                    "too_small",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::LengthMax => {
                let params = PyDict::new(py);
                params.set_item("maximum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item(
                    "origin",
                    if parsed.is_instance_of::<PyString>() {
                        "string"
                    } else {
                        "array"
                    },
                )?;
                (
                    "too_big",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Min => {
                let params = PyDict::new(py);
                params.set_item("minimum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item("origin", "number")?;
                (
                    "too_small",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Max => {
                let params = PyDict::new(py);
                params.set_item("maximum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item("origin", "number")?;
                (
                    "too_big",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::ExclusiveMin => {
                let params = PyDict::new(py);
                params.set_item("minimum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", false)?;
                params.set_item("origin", "number")?;
                (
                    "too_small",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::ExclusiveMax => {
                let params = PyDict::new(py);
                params.set_item("maximum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", false)?;
                params.set_item("origin", "number")?;
                (
                    "too_big",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::MultipleOf => {
                let params = PyDict::new(py);
                params.set_item("divisor", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("origin", "number")?;
                (
                    "not_multiple_of",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::StartsWith => {
                let params = PyDict::new(py);
                params.set_item("format", "starts_with")?;
                params.set_item("prefix", self.args[0].bind(py))?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::EndsWith => {
                let params = PyDict::new(py);
                params.set_item("format", "ends_with")?;
                params.set_item("suffix", self.args[0].bind(py))?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Includes => {
                let params = PyDict::new(py);
                params.set_item("format", "includes")?;
                params.set_item("substring", self.args[0].bind(py))?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Ipv6 => {
                let params = PyDict::new(py);
                params.set_item("format", "ipv6")?;
                (
                    "invalid_format",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Positive => {
                let params = PyDict::new(py);
                params.set_item("minimum", 0)?;
                params.set_item("inclusive", false)?;
                params.set_item("origin", "number")?;
                (
                    "too_small",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Integral => {
                let params = PyDict::new(py);
                params.set_item("expected", "integer")?;
                (
                    "invalid_type",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Regex => {
                let params = PyDict::new(py);
                params.set_item("format", self.args[2].bind(py).extract::<String>()?)?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            _ => unreachable!(),
        };
        ctx.fail_with(code, &message, path, params)
    }

    fn validate_json_fast(
        &self,
        py: Python<'_>,
        value: &Value,
        depth: usize,
    ) -> PyResult<Option<Py<PyAny>>> {
        if depth >= MAX_DEPTH {
            return Ok(None);
        }
        match self.kind {
            Kind::String => match value {
                Value::String(text) => Ok(Some(PyString::new(py, text).into_any().unbind())),
                _ => Ok(None),
            },
            Kind::Integer => match value {
                Value::Number(number) => {
                    if let Some(number) = number.as_i64() {
                        Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
                    } else if let Some(number) = number.as_u64() {
                        Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
                    } else {
                        Ok(None)
                    }
                }
                _ => Ok(None),
            },
            Kind::Boolean => match value {
                Value::Bool(boolean) => Ok(Some(
                    (*boolean).into_pyobject(py)?.to_owned().into_any().unbind(),
                )),
                _ => Ok(None),
            },
            Kind::None => {
                if value.is_null() {
                    Ok(Some(py.None()))
                } else {
                    Ok(None)
                }
            }
            Kind::Literal => {
                let expected = self.args[0].bind(py);
                if !json_literal_matches(value, expected)?.unwrap_or(false) {
                    return Ok(None);
                }
                json_scalar_to_py(py, value)
            }
            Kind::Enum => {
                let choices = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                for choice in choices.iter() {
                    match json_literal_matches(value, &choice)? {
                        Some(true) => return json_scalar_to_py(py, value),
                        Some(false) => {}
                        None => return Ok(None),
                    }
                }
                Ok(None)
            }
            Kind::Positive => {
                let positive = json_integer_i128(value).is_some_and(|number| number > 0);
                if !positive {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::Min | Kind::Max | Kind::ExclusiveMin | Kind::ExclusiveMax => {
                let Some(actual) = json_integer_i128(value) else {
                    return Ok(None);
                };
                let Some(bound) = python_int_i128(self.args[0].bind(py)) else {
                    return Ok(None);
                };
                let valid = match self.kind {
                    Kind::Min => actual >= bound,
                    Kind::Max => actual <= bound,
                    Kind::ExclusiveMin => actual > bound,
                    Kind::ExclusiveMax => actual < bound,
                    _ => unreachable!(),
                };
                if !valid {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::MultipleOf => {
                let Some(actual) = json_integer_i128(value) else {
                    return Ok(None);
                };
                let Some(divisor) = python_int_i128(self.args[0].bind(py)) else {
                    return Ok(None);
                };
                if divisor <= 0 || actual % divisor != 0 {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::Nullable => {
                if value.is_null() {
                    Ok(Some(py.None()))
                } else {
                    self.child_json_fast(py, value, depth)
                }
            }
            Kind::Optional => {
                if value.is_null() {
                    Ok(None)
                } else {
                    self.child_json_fast(py, value, depth)
                }
            }
            Kind::NonOptional => self.child_json_fast(py, value, depth),
            Kind::LengthMin | Kind::LengthMax => {
                let Ok(bound) = self.args[0].bind(py).extract::<usize>() else {
                    return Ok(None);
                };
                let length = match value {
                    Value::String(text) => text.chars().count(),
                    Value::Array(items) => items.len(),
                    _ => return Ok(None),
                };
                let valid = if matches!(self.kind, Kind::LengthMin) {
                    length >= bound
                } else {
                    length <= bound
                };
                if !valid {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::Array => {
                let Value::Array(items) = value else {
                    return Ok(None);
                };
                let mut result = Vec::with_capacity(items.len());
                for item in items {
                    let Some(parsed) = self.child_json_fast(py, item, depth)? else {
                        return Ok(None);
                    };
                    result.push(parsed);
                }
                Ok(Some(PyList::new(py, result)?.into_any().unbind()))
            }
            Kind::Object => {
                let Value::Object(map) = value else {
                    return Ok(None);
                };
                let keys = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let result = PyDict::new(py);
                let mut present = 0;
                for (index, key) in keys.iter().enumerate() {
                    let Ok(key_text) = key.cast::<PyString>().map_err(PyErr::from)?.to_str() else {
                        return Ok(None);
                    };
                    let child = self.children[index].try_borrow(py).map_err(PyErr::from)?;
                    let Some(raw) = map.get(key_text) else {
                        if matches!(child.kind, Kind::Optional) {
                            continue;
                        }
                        return Ok(None);
                    };
                    present += 1;
                    let Some(parsed) = child.validate_json_fast(py, raw, depth + 1)? else {
                        return Ok(None);
                    };
                    result.set_item(&key, parsed)?;
                }
                if self.unknown_keys == UnknownKeys::Strict && present != map.len() {
                    return Ok(None);
                }
                Ok(Some(result.into_any().unbind()))
            }
            _ => Ok(None),
        }
    }

    fn child_json_fast(
        &self,
        py: Python<'_>,
        value: &Value,
        depth: usize,
    ) -> PyResult<Option<Py<PyAny>>> {
        self.children[0]
            .try_borrow(py)
            .map_err(PyErr::from)?
            .validate_json_fast(py, value, depth + 1)
    }

    fn evaluate(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let missing = value.is(&ctx.missing);
        let keep = || Ok(value.clone().unbind());
        match self.kind {
            Kind::Cleared => Err(PyRuntimeError::new_err("Native schema has been cleared").into()),
            Kind::AsyncRefine
            | Kind::AsyncSuperRefine
            | Kind::AsyncTransform
            | Kind::AsyncPreprocess
            | Kind::AsyncDefaultFactory
            | Kind::AsyncCatch
            | Kind::AsyncOverwrite => Err(PyTypeError::new_err(
                "Async validation must be performed with parse_async()",
            )
            .into()),
            Kind::Optional if missing => keep(),
            Kind::Nullable if value.is_none() => keep(),
            Kind::Optional | Kind::Nullable => self.child(0, ctx, value, path, depth),
            Kind::NonOptional => {
                if missing {
                    ctx.fail("required", "Required", path)
                } else {
                    self.child(0, ctx, value, path, depth)
                }
            }
            Kind::Default | Kind::DefaultFactory => {
                if missing {
                    let default = if matches!(self.kind, Kind::Default) {
                        deepcopy(self.args[0].bind(py))?
                    } else {
                        self.args[0].bind(py).call0()?
                    };
                    self.child(0, ctx, &default, path, depth)
                } else {
                    self.child(0, ctx, value, path, depth)
                }
            }
            Kind::Lazy => {
                let key = self as *const Self as usize;
                let node = match ctx.lazy_store.last(py, key) {
                    Some(node) => node,
                    None => match ctx.lazy_store.find(py, key) {
                        Some(node) => {
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            node
                        }
                        None => {
                            let schema = self.args[0].bind(py).call0()?.getattr("_node")?;
                            let node: Py<NativeSchema> = schema
                                .cast_into::<NativeSchema>()
                                .map_err(PyErr::from)?
                                .unbind();
                            ctx.lazy_store.push(key, node.clone_ref(py));
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            node
                        }
                    },
                };
                node.try_borrow(py)
                    .map_err(PyErr::from)?
                    .validate(ctx, value, path, depth + 1)
            }
            Kind::Catch => match self.child(0, ctx, value, path, depth) {
                Ok(value) => Ok(value),
                Err(Error::Invalid(_)) => {
                    let fallback = self.args[0].bind(py);
                    if fallback.is_callable() {
                        Ok(fallback.call0()?.unbind())
                    } else {
                        Ok(deepcopy(fallback)?.unbind())
                    }
                }
                Err(Error::Python(error)) => Err(Error::Python(error)),
            },
            Kind::Coerce => {
                if missing {
                    return self.child(0, ctx, value, path, depth);
                }
                let target: String = self.args[0].bind(py).extract()?;
                let Some(converted) = coerce_value(py, &target, value)? else {
                    let expected = format!("coercible {target}");
                    let params = PyDict::new(py);
                    params.set_item("expected", &expected)?;
                    return ctx.fail_with(
                        "invalid_type",
                        &format!("Expected {expected}"),
                        path,
                        params,
                    );
                };
                self.child(0, ctx, &converted, path, depth)
            }
            Kind::Preprocess => {
                let processed = self.args[0].bind(py).call1((value,))?;
                self.child(0, ctx, &processed, path, depth)
            }
            Kind::Refine => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                if self.args[0].bind(py).call1((parsed,))?.is_truthy()? {
                    return Ok(parsed.clone().unbind());
                }
                let message: String = self.args[1].bind(py).extract()?;
                let relative = self.args[2].bind(py);
                let mut issue_path = path
                    .iter()
                    .map(|part| part.clone_ref(py))
                    .collect::<Vec<_>>();
                for part in relative.try_iter()? {
                    issue_path.push(PathPart::Value(part?.unbind()));
                }
                ctx.fail("custom", &message, &issue_path)
            }
            Kind::SuperRefine => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let result = self.args[0].bind(py).call1((parsed.bind(py),))?;
                if result.is_none() {
                    return Ok(parsed);
                }
                let mut issues = Vec::new();
                if result.is_instance(&ctx.issue_type)? {
                    issues.push(Issue::from_python(&result)?);
                } else if result.is_instance_of::<PyList>() || result.is_instance_of::<PyTuple>() {
                    for item in result.try_iter()? {
                        let item = item?;
                        if !item.is_instance(&ctx.issue_type)? {
                            return Err(PyTypeError::new_err(
                                "super_refine callback returned a non-Issue value",
                            )
                            .into());
                        }
                        issues.push(Issue::from_python(&item)?);
                    }
                } else {
                    return Err(PyTypeError::new_err(
                        "super_refine callback must return Issue, a list or tuple of Issue, or None",
                    )
                    .into());
                }
                if issues.is_empty() {
                    return Ok(parsed);
                }
                for issue in &mut issues {
                    let mut absolute = path
                        .iter()
                        .map(|part| part.clone_ref(py))
                        .collect::<Vec<_>>();
                    absolute.extend(issue.path.iter().map(|part| part.clone_ref(py)));
                    issue.path = absolute;
                }
                Err(Error::Invalid(issues))
            }
            Kind::Transform
            | Kind::Overwrite
            | Kind::LengthMin
            | Kind::LengthMax
            | Kind::Min
            | Kind::Max
            | Kind::ExclusiveMin
            | Kind::ExclusiveMax
            | Kind::MultipleOf
            | Kind::Positive
            | Kind::Integral
            | Kind::Regex
            | Kind::StartsWith
            | Kind::EndsWith
            | Kind::Includes
            | Kind::Ipv6
            | Kind::Trim
            | Kind::Lower
            | Kind::Upper => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                self.evaluate_post_child(ctx, parsed, path)
            }
            Kind::Pipe => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                self.child(1, ctx, parsed.bind(py), path, depth)
            }
            _ if missing && !matches!(self.kind, Kind::Union) => {
                ctx.fail("required", "Required", path)
            }
            Kind::StringBool => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                let text: String = parsed.extract()?;
                let case_sensitive: bool = self.args[2].bind(py).extract()?;
                let normalized = if case_sensitive {
                    text
                } else {
                    text.to_lowercase()
                };
                for item in self.args[0].bind(py).try_iter()? {
                    let candidate: String = item?.extract()?;
                    let candidate = if case_sensitive {
                        candidate
                    } else {
                        candidate.to_lowercase()
                    };
                    if normalized == candidate {
                        return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
                    }
                }
                for item in self.args[1].bind(py).try_iter()? {
                    let candidate: String = item?.extract()?;
                    let candidate = if case_sensitive {
                        candidate
                    } else {
                        candidate.to_lowercase()
                    };
                    if normalized == candidate {
                        return Ok(false.into_pyobject(py)?.to_owned().into_any().unbind());
                    }
                }
                let message: String = self.args[3].bind(py).extract()?;
                let truthy = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let falsy = self.args[1]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let values = truthy
                    .iter()
                    .chain(falsy.iter())
                    .map(Bound::unbind)
                    .collect::<Vec<_>>();
                let values = PyTuple::new(py, values)?;
                let params = PyDict::new(py);
                params.set_item("values", values)?;
                ctx.fail_with("invalid_value", &message, path, params)
            }
            Kind::ExactInstance | Kind::NativeEnum => {
                let cls = self.args[0].bind(py);
                if value.get_type().is(cls) {
                    keep()
                } else {
                    let name: String = cls.getattr("__name__")?.extract()?;
                    let params = PyDict::new(py);
                    params.set_item("expected", &name)?;
                    ctx.fail_with("invalid_type", &format!("Expected {name}"), path, params)
                }
            }
            Kind::Json => {
                if is_json_value(value, 0)? {
                    keep()
                } else {
                    let params = PyDict::new(py);
                    params.set_item("expected", "JSON value")?;
                    ctx.fail_with("invalid_type", "Expected JSON value", path, params)
                }
            }
            Kind::Enum => {
                let choices = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                for choice in choices.iter() {
                    if value.get_type().is(choice.get_type()) && value.eq(&choice)? {
                        return keep();
                    }
                }
                if let Some(template) = &self.error_template {
                    return ctx.fail_template(template, path);
                }
                let params = PyDict::new(py);
                let values = PyTuple::new(
                    py,
                    choices
                        .iter()
                        .map(|choice| safe_param(py, &choice).map(Bound::unbind))
                        .collect::<PyResult<Vec<_>>>()?,
                )?;
                params.set_item("values", values)?;
                let message = enum_message(choices)?;
                ctx.fail_with("invalid_value", &message, path, params)
            }
            Kind::Literal => {
                let expected = self.args[0].bind(py);
                if value.get_type().is(expected.get_type()) && value.eq(expected)? {
                    return keep();
                }
                if let Some(template) = &self.error_template {
                    return ctx.fail_template(template, path);
                }
                let message = format!("Expected {}", literal_description(expected)?);
                let params = PyDict::new(py);
                let values = PyTuple::new(py, vec![safe_param(py, expected)?.unbind()])?;
                params.set_item("values", values)?;
                ctx.fail_with("invalid_value", &message, path, params)
            }
            Kind::String
            | Kind::Number
            | Kind::Integer
            | Kind::Boolean
            | Kind::Unknown
            | Kind::None
            | Kind::Instance => {
                let (valid, name) = match self.kind {
                    Kind::String => (value.is_instance_of::<PyString>(), "string".into()),
                    Kind::Integer => (value.is_exact_instance_of::<PyInt>(), "integer".into()),
                    Kind::Number => (
                        value.is_exact_instance_of::<PyInt>()
                            || (value.is_exact_instance_of::<PyFloat>()
                                && value.extract::<f64>()?.is_finite()),
                        "finite number".into(),
                    ),
                    Kind::Boolean => (value.is_exact_instance_of::<PyBool>(), "boolean".into()),
                    Kind::Unknown => (true, "value".into()),
                    Kind::None => (value.is_none(), "None".into()),
                    Kind::Instance => {
                        let cls = self.args[0].bind(py);
                        (
                            value.is_instance(cls)?,
                            cls.getattr("__name__")?.extract::<String>()?,
                        )
                    }
                    _ => unreachable!(),
                };
                if valid {
                    keep()
                } else if let Some(template) = &self.error_template {
                    ctx.fail_template(template, path)
                } else {
                    let params = PyDict::new(py);
                    params.set_item("expected", &name)?;
                    ctx.fail_with("invalid_type", &format!("Expected {name}"), path, params)
                }
            }
            Kind::Array | Kind::Tuple => {
                let is_tuple = matches!(self.kind, Kind::Tuple);
                if !value.is_instance_of::<PyList>()
                    && !(is_tuple && value.is_instance_of::<PyTuple>())
                {
                    let expected = if is_tuple { "tuple or list" } else { "list" };
                    let params = PyDict::new(py);
                    params.set_item("expected", expected)?;
                    return ctx.fail_with(
                        "invalid_type",
                        &format!("Expected {expected}"),
                        path,
                        params,
                    );
                }
                let exact_list = if !is_tuple && value.is_exact_instance_of::<PyList>() {
                    Some(value.cast::<PyList>().map_err(PyErr::from)?)
                } else {
                    None
                };
                if let Some(list) = &exact_list {
                    let child_kind = self.children[0].try_borrow(py).map_err(PyErr::from)?.kind;
                    if is_primitive_kind(child_kind) {
                        if !list.is_empty() && depth + 1 >= MAX_DEPTH {
                            return Err(PyRecursionError::new_err(
                                "Native schema depth exceeds 256 nodes",
                            )
                            .into());
                        }
                        if let Some(parsed) = fast_array_list(py, list, child_kind)? {
                            return Ok(parsed);
                        }
                    }
                }
                // Snapshot containers: a callback can mutate the original without
                // invalidating Rust's traversal or Python dictionary iterators.
                let mut items = ItemBuffer::new();
                if let Some(list) = &exact_list {
                    for item in list.iter() {
                        items.push(item);
                    }
                } else if is_tuple && value.is_exact_instance_of::<PyTuple>() {
                    for item in value.cast::<PyTuple>().map_err(PyErr::from)?.iter() {
                        items.push(item);
                    }
                } else {
                    for item in value.try_iter()? {
                        items.push(item?);
                    }
                }
                if !is_tuple && !items.is_empty() && depth + 1 >= MAX_DEPTH {
                    return Err(
                        PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into(),
                    );
                }
                let fixed = self.children.len();
                let rest_node = if is_tuple {
                    let rest = self.args[0].bind(py);
                    if rest.is_none() {
                        if items.len() != fixed {
                            let params = PyDict::new(py);
                            params.set_item("expected", fixed)?;
                            return ctx.fail_with(
                                "invalid_length",
                                &format!("Expected {fixed} items"),
                                path,
                                params,
                            );
                        }
                        None
                    } else {
                        if items.len() < fixed {
                            let params = PyDict::new(py);
                            params.set_item("minimum", fixed)?;
                            params.set_item("inclusive", true)?;
                            params.set_item("origin", "array")?;
                            return ctx.fail_with(
                                "too_small",
                                &format!("Expected at least {fixed} items"),
                                path,
                                params,
                            );
                        }
                        Some(rest.extract::<PyRef<'_, Self>>().map_err(PyErr::from)?)
                    }
                } else {
                    None
                };
                let mut result = Vec::with_capacity(items.len());
                let mut issues = Vec::new();
                for index in 0..items.len() {
                    let raw = items.get(index).expect("item");
                    path.push(PathPart::Index(index));
                    let parsed = if is_tuple && index >= fixed {
                        rest_node
                            .as_ref()
                            .expect("rest schema")
                            .validate(ctx, raw, path, depth + 1)
                    } else {
                        self.child(if is_tuple { index } else { 0 }, ctx, raw, path, depth)
                    };
                    path.pop();
                    match parsed {
                        Ok(value) => result.push(value),
                        Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                        Err(error) => return Err(error),
                    }
                }
                if !issues.is_empty() {
                    return Err(Error::Invalid(issues));
                }
                if is_tuple {
                    Ok(PyTuple::new(py, result)?.into_any().unbind())
                } else {
                    Ok(PyList::new(py, result)?.into_any().unbind())
                }
            }
            Kind::Object | Kind::Record => {
                let Ok(dict) = value.cast::<PyDict>() else {
                    let expected = if matches!(self.kind, Kind::Object) {
                        "dict with string keys"
                    } else {
                        "dict"
                    };
                    let params = PyDict::new(py);
                    params.set_item("expected", expected)?;
                    return ctx.fail_with(
                        "invalid_type",
                        &format!("Expected {expected}"),
                        path,
                        params,
                    );
                };
                let result = PyDict::new(py);
                let mut issues = Vec::new();
                if matches!(self.kind, Kind::Object) {
                    let keys = self.args[0]
                        .bind(py)
                        .cast::<PyTuple>()
                        .map_err(PyErr::from)?;
                    const INLINE_VALUES: usize = 4;
                    let policy = self.unknown_keys;
                    let mut inline_values: [Option<Bound<'_, PyAny>>; INLINE_VALUES] =
                        std::array::from_fn(|_| None);
                    let mut heap_values =
                        Vec::with_capacity(keys.len().saturating_sub(INLINE_VALUES));
                    let mut present = 0;
                    for (index, key) in keys.iter().enumerate() {
                        let value = dict.get_item(&key)?;
                        if value.is_some() {
                            present += 1;
                        }
                        if index < INLINE_VALUES {
                            inline_values[index] = value;
                        } else {
                            heap_values.push(value);
                        }
                    }
                    let snapshot = if present == dict.len() {
                        None
                    } else {
                        Some(dict.copy()?)
                    };
                    for (index, key) in keys.iter().enumerate() {
                        let value = if index < INLINE_VALUES {
                            inline_values[index].take()
                        } else {
                            heap_values[index - INLINE_VALUES].take()
                        };
                        let is_present = value.is_some();
                        let raw = value.unwrap_or_else(|| ctx.missing.clone());
                        let child = self.children[index].try_borrow(py).map_err(PyErr::from)?;
                        if is_present
                            && depth + 1 < MAX_DEPTH
                            && is_primitive_kind(child.kind)
                            && primitive_matches(&raw, child.kind)?
                        {
                            result.set_item(&key, &raw)?;
                            continue;
                        }
                        path.push(PathPart::Value(key.clone().unbind()));
                        let parsed = child.validate(ctx, &raw, path, depth + 1);
                        path.pop();
                        match parsed {
                            Ok(value) if !value.bind(py).is(&ctx.missing) => {
                                result.set_item(&key, value)?
                            }
                            Ok(_) => {}
                            Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                            Err(error) => return Err(error),
                        }
                    }
                    if let Some(snapshot) = snapshot {
                        let known = PyDict::new(py);
                        for key in keys.iter() {
                            known.set_item(&key, true)?;
                        }
                        for (key, raw) in snapshot.iter() {
                            if !key.is_instance_of::<PyString>() {
                                let params = PyDict::new(py);
                                params.set_item("expected", "string")?;
                                issues.push(Issue::new(
                                    py,
                                    "invalid_key",
                                    "Object keys must be strings",
                                    path,
                                    params.into_any(),
                                ));
                            } else if !known.contains(&key)? {
                                if policy == UnknownKeys::Strict {
                                    let params = PyDict::new(py);
                                    params.set_item(
                                        "keys",
                                        PyTuple::new(py, vec![key.clone().unbind()])?,
                                    )?;
                                    path.push(PathPart::Value(key.clone().unbind()));
                                    issues.push(Issue::new(
                                        py,
                                        "unrecognized_key",
                                        "Unrecognized key",
                                        path,
                                        params.into_any(),
                                    ));
                                    path.pop();
                                } else if policy == UnknownKeys::Passthrough {
                                    result.set_item(key, deepcopy(&raw)?)?;
                                } else if policy == UnknownKeys::Catchall {
                                    let catchall = self.args[2].bind(py);
                                    let node: PyRef<'_, Self> =
                                        catchall.extract().map_err(PyErr::from)?;
                                    path.push(PathPart::Value(key.clone().unbind()));
                                    let parsed = node.validate(ctx, &raw, path, depth + 1);
                                    path.pop();
                                    match parsed {
                                        Ok(value) => result.set_item(key, value)?,
                                        Err(Error::Invalid(mut errors)) => {
                                            issues.append(&mut errors);
                                        }
                                        Err(error) => return Err(error),
                                    }
                                }
                            }
                        }
                    }
                } else {
                    let snapshot = dict.copy()?;
                    let key_schema = self.args[0].bind(py);
                    let key_node = if key_schema.is_none() {
                        None
                    } else {
                        Some(
                            key_schema
                                .extract::<PyRef<'_, Self>>()
                                .map_err(PyErr::from)?,
                        )
                    };
                    for (key, raw) in snapshot.iter() {
                        let parsed_key = if let Some(node) = key_node.as_ref() {
                            match node.validate(ctx, &key, path, depth + 1) {
                                Ok(key) => key,
                                Err(Error::Invalid(mut errors)) => {
                                    issues.append(&mut errors);
                                    continue;
                                }
                                Err(error) => return Err(error),
                            }
                        } else {
                            if !key.is_instance_of::<PyString>() {
                                let params = PyDict::new(py);
                                params.set_item("expected", "string")?;
                                issues.push(Issue::new(
                                    py,
                                    "invalid_key",
                                    "Record keys must be strings",
                                    path,
                                    params.into_any(),
                                ));
                                continue;
                            }
                            key.clone().unbind()
                        };
                        path.push(PathPart::Value(parsed_key.clone_ref(py)));
                        let parsed = self.child(0, ctx, &raw, path, depth);
                        path.pop();
                        match parsed {
                            Ok(value) => result.set_item(parsed_key.bind(py), value)?,
                            Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                            Err(error) => return Err(error),
                        }
                    }
                }
                if !issues.is_empty() {
                    return Err(Error::Invalid(issues));
                }
                Ok(result.into_any().unbind())
            }
            Kind::DiscriminatedUnion => {
                let Ok(dict) = value.cast::<PyDict>() else {
                    let params = PyDict::new(py);
                    params.set_item("expected", "dict with string keys")?;
                    return ctx.fail_with(
                        "invalid_type",
                        "Expected dict with string keys",
                        path,
                        params,
                    );
                };
                let key = self.args[0].bind(py);
                let options = self.args[1]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let Some(raw) = dict.get_item(key)? else {
                    path.push(PathPart::Value(self.args[0].clone_ref(py)));
                    let result = ctx.fail("required", "Required", path);
                    path.pop();
                    return result;
                };
                let mut selected = None;
                for (index, option) in options.iter().enumerate() {
                    if raw.get_type().is(option.get_type()) && raw.eq(&option)? {
                        selected = Some(index);
                        break;
                    }
                }
                let Some(index) = selected else {
                    let message = enum_message(options)?;
                    let params = PyDict::new(py);
                    params.set_item("values", options)?;
                    path.push(PathPart::Value(self.args[0].clone_ref(py)));
                    let result = ctx.fail_with("invalid_value", &message, path, params);
                    path.pop();
                    return result;
                };
                self.child(index, ctx, value, path, depth)
            }
            Kind::Intersection => {
                let left = self.child(0, ctx, value, path, depth);
                let right = self.child(1, ctx, value, path, depth);
                match (left, right) {
                    (Ok(left), Ok(right)) => {
                        let left = left.bind(py);
                        let right = right.bind(py);
                        if !left.is_instance_of::<PyDict>() || !right.is_instance_of::<PyDict>() {
                            return ctx.fail(
                                "invalid_intersection_types",
                                "Intersection outputs must be dictionaries",
                                path,
                            );
                        }
                        let merged = merge_dicts(
                            left.cast::<PyDict>().map_err(PyErr::from)?,
                            right.cast::<PyDict>().map_err(PyErr::from)?,
                            0,
                        )?;
                        Ok(merged)
                    }
                    (
                        Err(Error::Invalid(mut left_issues)),
                        Err(Error::Invalid(mut right_issues)),
                    ) => {
                        left_issues.append(&mut right_issues);
                        Err(Error::Invalid(left_issues))
                    }
                    (Err(Error::Invalid(issues)), Ok(_)) | (Ok(_), Err(Error::Invalid(issues))) => {
                        Err(Error::Invalid(issues))
                    }
                    (Err(Error::Python(error)), _) | (_, Err(Error::Python(error))) => {
                        Err(Error::Python(error))
                    }
                }
            }
            Kind::Union => {
                let mut branches = Vec::new();
                for index in 0..self.children.len() {
                    match self.child(index, ctx, value, path, depth) {
                        Ok(value) => return Ok(value),
                        Err(Error::Invalid(issues)) => branches.push(issues),
                        Err(error) => return Err(error),
                    }
                }
                let mut issue = Issue::simple(py, "invalid_union", "No union branch matched", path);
                issue.branches = branches;
                Err(Error::Invalid(vec![issue]))
            }
        }
    }
}

fn literal_description(value: &Bound<'_, PyAny>) -> PyResult<String> {
    match value.repr() {
        Ok(text) => text.extract(),
        Err(error)
            if value.is_exact_instance_of::<PyInt>()
                && error.is_instance_of::<PyValueError>(value.py()) =>
        {
            let bits: usize = value.call_method0("bit_length")?.extract()?;
            Ok(format!("<integer with {bits} bits>"))
        }
        Err(error) => Err(error),
    }
}

const INLINE_ITEMS: usize = 4;

enum ItemBuffer<'py> {
    Inline([Option<Bound<'py, PyAny>>; INLINE_ITEMS], usize),
    Heap(Vec<Bound<'py, PyAny>>),
}

impl<'py> ItemBuffer<'py> {
    fn new() -> Self {
        Self::Inline(std::array::from_fn(|_| None), 0)
    }

    fn push(&mut self, item: Bound<'py, PyAny>) {
        if let Self::Inline(slots, len) = self {
            if *len < INLINE_ITEMS {
                slots[*len] = Some(item);
                *len += 1;
                return;
            }
            let mut values = Vec::with_capacity(INLINE_ITEMS + 1);
            for slot in slots.iter_mut() {
                if let Some(value) = slot.take() {
                    values.push(value);
                }
            }
            values.push(item);
            *self = Self::Heap(values);
            return;
        }
        if let Self::Heap(values) = self {
            values.push(item);
        }
    }

    fn len(&self) -> usize {
        match self {
            Self::Inline(_, len) => *len,
            Self::Heap(values) => values.len(),
        }
    }

    fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn get(&self, index: usize) -> Option<&Bound<'py, PyAny>> {
        match self {
            Self::Inline(slots, len) if index < *len => slots[index].as_ref(),
            Self::Heap(values) => values.get(index),
            _ => None,
        }
    }
}

fn is_primitive_kind(kind: Kind) -> bool {
    matches!(
        kind,
        Kind::String | Kind::Integer | Kind::Number | Kind::Boolean | Kind::None | Kind::Unknown
    )
}

fn json_integer_i128(value: &Value) -> Option<i128> {
    let Value::Number(number) = value else {
        return None;
    };
    number
        .as_i64()
        .map(i128::from)
        .or_else(|| number.as_u64().map(i128::from))
}

fn python_int_i128(value: &Bound<'_, PyAny>) -> Option<i128> {
    value
        .extract::<i64>()
        .ok()
        .map(i128::from)
        .or_else(|| value.extract::<u64>().ok().map(i128::from))
}

fn json_scalar_to_py(py: Python<'_>, value: &Value) -> PyResult<Option<Py<PyAny>>> {
    match value {
        Value::Null => Ok(Some(py.None())),
        Value::Bool(boolean) => Ok(Some(
            (*boolean).into_pyobject(py)?.to_owned().into_any().unbind(),
        )),
        Value::Number(number) => {
            if let Some(number) = number.as_i64() {
                Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
            } else if let Some(number) = number.as_u64() {
                Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
            } else {
                Ok(None)
            }
        }
        Value::String(text) => Ok(Some(PyString::new(py, text).into_any().unbind())),
        _ => Ok(None),
    }
}

fn json_literal_matches(value: &Value, expected: &Bound<'_, PyAny>) -> PyResult<Option<bool>> {
    if expected.is_exact_instance_of::<PyBool>() {
        let expected: bool = expected.extract()?;
        return Ok(Some(
            matches!(value, Value::Bool(boolean) if *boolean == expected),
        ));
    }
    if expected.is_exact_instance_of::<PyInt>() {
        let Some(actual) = json_integer_i128(value) else {
            return Ok(match value {
                Value::Number(number) if number.as_i64().is_none() && number.as_u64().is_none() => {
                    None
                }
                _ => Some(false),
            });
        };
        let Some(expected) = python_int_i128(expected) else {
            return Ok(None);
        };
        return Ok(Some(actual == expected));
    }
    if expected.is_exact_instance_of::<PyFloat>() {
        return Ok(None);
    }
    if expected.is_none() {
        return Ok(Some(value.is_null()));
    }
    if expected.is_exact_instance_of::<PyString>() {
        let expected = expected.cast::<PyString>().map_err(PyErr::from)?.to_str()?;
        return Ok(Some(
            matches!(value, Value::String(text) if text == expected),
        ));
    }
    Ok(None)
}

fn build_error_template(
    py: Python<'_>,
    kind: Kind,
    children: &[Py<NativeSchema>],
    args: &[Py<PyAny>],
) -> PyResult<Option<ErrorTemplate>> {
    let template = |code: &str, message: String, params: Bound<'_, PyDict>| -> ErrorTemplate {
        ErrorTemplate {
            code: Arc::from(code),
            message: Arc::from(message),
            params: params.into_any().unbind(),
        }
    };
    let template = match kind {
        Kind::Positive => {
            let params = PyDict::new(py);
            params.set_item("minimum", 0)?;
            params.set_item("inclusive", false)?;
            params.set_item("origin", "number")?;
            template("too_small", args[0].bind(py).extract::<String>()?, params)
        }
        Kind::LengthMin | Kind::LengthMax => {
            let origin = match children[0].try_borrow(py).map_err(PyErr::from)?.kind {
                Kind::String => "string",
                Kind::Array => "array",
                _ => return Ok(None),
            };
            let params = PyDict::new(py);
            if matches!(kind, Kind::LengthMin) {
                params.set_item("minimum", safe_param(py, args[0].bind(py))?)?;
            } else {
                params.set_item("maximum", safe_param(py, args[0].bind(py))?)?;
            }
            params.set_item("inclusive", true)?;
            params.set_item("origin", origin)?;
            template(
                if matches!(kind, Kind::LengthMin) {
                    "too_small"
                } else {
                    "too_big"
                },
                args[1].bind(py).extract::<String>()?,
                params,
            )
        }
        Kind::Min | Kind::Max | Kind::ExclusiveMin | Kind::ExclusiveMax => {
            let params = PyDict::new(py);
            match kind {
                Kind::Min => params.set_item("minimum", safe_param(py, args[0].bind(py))?)?,
                Kind::Max => params.set_item("maximum", safe_param(py, args[0].bind(py))?)?,
                Kind::ExclusiveMin => {
                    params.set_item("minimum", safe_param(py, args[0].bind(py))?)?
                }
                Kind::ExclusiveMax => {
                    params.set_item("maximum", safe_param(py, args[0].bind(py))?)?
                }
                _ => unreachable!(),
            }
            params.set_item("inclusive", matches!(kind, Kind::Min | Kind::Max))?;
            params.set_item("origin", "number")?;
            template(
                if matches!(kind, Kind::Min | Kind::ExclusiveMin) {
                    "too_small"
                } else {
                    "too_big"
                },
                args[1].bind(py).extract::<String>()?,
                params,
            )
        }
        Kind::MultipleOf => {
            let params = PyDict::new(py);
            params.set_item("divisor", safe_param(py, args[0].bind(py))?)?;
            params.set_item("origin", "number")?;
            template(
                "not_multiple_of",
                args[1].bind(py).extract::<String>()?,
                params,
            )
        }
        Kind::Literal => {
            let expected = args[0].bind(py);
            let message = format!("Expected {}", literal_description(expected)?);
            let params = PyDict::new(py);
            params.set_item(
                "values",
                PyTuple::new(py, vec![safe_param(py, expected)?.unbind()])?,
            )?;
            template("invalid_value", message, params)
        }
        Kind::Enum => {
            let choices = args[0].bind(py).cast::<PyTuple>().map_err(PyErr::from)?;
            let message = enum_message(choices)?;
            let params = PyDict::new(py);
            params.set_item(
                "values",
                PyTuple::new(
                    py,
                    choices
                        .iter()
                        .map(|choice| safe_param(py, &choice).map(Bound::unbind))
                        .collect::<PyResult<Vec<_>>>()?,
                )?,
            )?;
            template("invalid_value", message, params)
        }
        Kind::String | Kind::Integer | Kind::Number | Kind::Boolean | Kind::None => {
            let name = match kind {
                Kind::String => "string",
                Kind::Integer => "integer",
                Kind::Number => "finite number",
                Kind::Boolean => "boolean",
                Kind::None => "None",
                _ => unreachable!(),
            };
            let params = PyDict::new(py);
            params.set_item("expected", name)?;
            template("invalid_type", format!("Expected {name}"), params)
        }
        _ => return Ok(None),
    };
    Ok(Some(template))
}

fn parse_json_value(data: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
    if data.is_instance_of::<PyString>() {
        let text = data.cast::<PyString>().map_err(PyErr::from)?.to_str()?;
        Ok(serde_json::from_str(text).ok())
    } else if data.is_instance_of::<PyBytes>() {
        let bytes = data.cast::<PyBytes>().map_err(PyErr::from)?.as_bytes();
        Ok(serde_json::from_slice(bytes).ok())
    } else {
        Ok(None)
    }
}

fn primitive_matches(item: &Bound<'_, PyAny>, kind: Kind) -> PyResult<bool> {
    Ok(match kind {
        Kind::String => item.is_instance_of::<PyString>(),
        Kind::Integer => item.is_exact_instance_of::<PyInt>(),
        Kind::Number => {
            item.is_exact_instance_of::<PyInt>()
                || (item.is_exact_instance_of::<PyFloat>() && item.extract::<f64>()?.is_finite())
        }
        Kind::Boolean => item.is_exact_instance_of::<PyBool>(),
        Kind::None => item.is_none(),
        Kind::Unknown => true,
        _ => false,
    })
}

fn fast_array_list(
    py: Python<'_>,
    list: &Bound<'_, PyList>,
    kind: Kind,
) -> PyResult<Option<Py<PyAny>>> {
    let mut parsed = Vec::with_capacity(list.len());
    for item in list.iter() {
        if !primitive_matches(&item, kind)? {
            return Ok(None);
        }
        parsed.push(item.unbind());
    }
    Ok(Some(PyList::new(py, parsed)?.into_any().unbind()))
}

fn is_multiple_of(value: &Bound<'_, PyAny>, divisor: &Bound<'_, PyAny>) -> PyResult<bool> {
    if divisor.is_exact_instance_of::<PyBool>() {
        return Ok(false);
    }
    if divisor.is_exact_instance_of::<PyInt>() && divisor.eq(0)? {
        return Ok(false);
    }
    if value.is_exact_instance_of::<PyInt>() && divisor.is_exact_instance_of::<PyInt>() {
        return Ok(!value.rem(divisor)?.is_truthy()?);
    }
    if value.is_exact_instance_of::<PyInt>() && divisor.is_exact_instance_of::<PyFloat>() {
        let divisor_float: f64 = divisor.extract()?;
        if divisor_float.is_finite() && divisor_float > 0.0 && divisor_float.fract() == 0.0 {
            // `i64::MAX as f64` rounds up to 2**63, so values at or above it
            // must use an exact Python integer conversion instead of `as i64`.
            if divisor_float < i64::MAX as f64 {
                let divisor_int = (divisor_float as i64).into_pyobject(value.py())?.into_any();
                return Ok(!value.rem(&divisor_int)?.is_truthy()?);
            }
            let ratio = divisor.call_method0("as_integer_ratio")?;
            let divisor_int = ratio.get_item(0)?;
            return Ok(!value.rem(&divisor_int)?.is_truthy()?);
        }
    }

    let value_float: f64 = match value.extract() {
        Ok(value) => value,
        Err(_) => return Ok(false),
    };
    let divisor_float: f64 = match divisor.extract() {
        Ok(divisor) => divisor,
        Err(_) => return Ok(false),
    };
    if !value_float.is_finite() || !divisor_float.is_finite() || divisor_float <= 0.0 {
        return Ok(false);
    }
    let quotient = value_float / divisor_float;
    if !quotient.is_finite() {
        return Ok(false);
    }
    Ok((quotient - quotient.round()).abs() <= 8.0 * f64::EPSILON * quotient.abs().max(1.0))
}

fn safe_param<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    match value.str() {
        Ok(_) => Ok(value.clone()),
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            Ok(literal_description(value)?.into_pyobject(py)?.into_any())
        }
        Err(error) => Err(error),
    }
}

fn enum_message(choices: &Bound<'_, PyTuple>) -> PyResult<String> {
    const LIMIT: usize = 10;
    let mut parts = Vec::with_capacity(choices.len().min(LIMIT));
    for (index, choice) in choices.iter().enumerate() {
        if index == LIMIT {
            parts.push("...".to_string());
            break;
        }
        parts.push(literal_description(&choice)?);
    }
    Ok(format!("Expected one of: {}", parts.join(", ")))
}

fn coerce_value<'py>(
    py: Python<'py>,
    target: &str,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    match target {
        "string" => {
            if value.is_instance_of::<PyString>() {
                return Ok(Some(value.clone()));
            }
            if value.is_exact_instance_of::<PyInt>() {
                return Ok(Some(value.str()?.into_any()));
            }
            if value.is_exact_instance_of::<PyFloat>() {
                let number: f64 = value.extract()?;
                if number.is_finite() {
                    return Ok(Some(value.str()?.into_any()));
                }
            }
            Ok(None)
        }
        "number" => {
            if value.is_exact_instance_of::<PyInt>()
                || (value.is_exact_instance_of::<PyFloat>() && value.extract::<f64>()?.is_finite())
            {
                return Ok(Some(value.clone()));
            }
            if value.is_instance_of::<PyString>() {
                return coerce_number(py, value);
            }
            Ok(None)
        }
        "integer" => {
            if value.is_exact_instance_of::<PyInt>() {
                return Ok(Some(value.clone()));
            }
            if value.is_exact_instance_of::<PyFloat>() {
                let number: f64 = value.extract()?;
                if number.is_finite() && number.fract() == 0.0 {
                    let parsed = py.import("builtins")?.getattr("int")?.call1((value,))?;
                    return Ok(Some(parsed));
                }
            }
            if value.is_instance_of::<PyString>() {
                return coerce_integer(py, value);
            }
            Ok(None)
        }
        "boolean" => coerce_boolean(py, value),
        _ => Ok(None),
    }
}

fn coerce_number<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let text: String = value.call_method0("strip")?.extract()?;
    if text.is_empty() {
        return Ok(None);
    }
    let builtins = py.import("builtins")?;
    match builtins.getattr("int")?.call1((&text,)) {
        Ok(parsed) => return Ok(Some(parsed)),
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyTypeError>(py) => {}
        Err(error) => return Err(error),
    }
    match builtins.getattr("float")?.call1((&text,)) {
        Ok(parsed) => {
            let number: f64 = parsed.extract()?;
            if number.is_finite() {
                Ok(Some(parsed))
            } else {
                Ok(None)
            }
        }
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyTypeError>(py) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn coerce_integer<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let text: String = value.call_method0("strip")?.extract()?;
    if text.is_empty() {
        return Ok(None);
    }
    match py.import("builtins")?.getattr("int")?.call1((&text,)) {
        Ok(parsed) => Ok(Some(parsed)),
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyTypeError>(py) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn coerce_boolean<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if value.is_exact_instance_of::<PyBool>() {
        return Ok(Some(value.clone()));
    }
    if value.is_exact_instance_of::<PyInt>() {
        if value.eq(0)? {
            return Ok(Some(false.into_pyobject(py)?.to_owned().into_any()));
        }
        if value.eq(1)? {
            return Ok(Some(true.into_pyobject(py)?.to_owned().into_any()));
        }
        return Ok(None);
    }
    if value.is_instance_of::<PyString>() {
        let normalized: String = value
            .call_method0("strip")?
            .extract::<String>()?
            .to_lowercase();
        if matches!(normalized.as_str(), "true" | "1" | "yes" | "on") {
            return Ok(Some(true.into_pyobject(py)?.to_owned().into_any()));
        }
        if matches!(normalized.as_str(), "false" | "0" | "no" | "off") {
            return Ok(Some(false.into_pyobject(py)?.to_owned().into_any()));
        }
    }
    Ok(None)
}

fn is_json_value(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<bool> {
    if depth >= MAX_DEPTH {
        return Err(PyRecursionError::new_err(
            "JSON value depth exceeds 256 nodes",
        ));
    }
    if value.is_none()
        || value.is_instance_of::<PyBool>()
        || value.is_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
    {
        return Ok(true);
    }
    if value.is_exact_instance_of::<PyFloat>() {
        return Ok(value.extract::<f64>()?.is_finite());
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        for item in value.try_iter()? {
            if !is_json_value(&item?, depth + 1)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter() {
            if !key.is_instance_of::<PyString>() || !is_json_value(&item, depth + 1)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    Ok(false)
}

fn merge_dicts<'py>(
    left: &Bound<'py, PyDict>,
    right: &Bound<'py, PyDict>,
    depth: usize,
) -> PyResult<Py<PyAny>> {
    if depth >= MAX_DEPTH {
        return Err(PyRecursionError::new_err(
            "Intersection merge depth exceeds 256 levels",
        ));
    }
    let result = left.copy()?;
    for (key, right_value) in right.iter() {
        let merged = match result.get_item(&key)? {
            Some(left_value)
                if left_value.is_instance_of::<PyDict>()
                    && right_value.is_instance_of::<PyDict>() =>
            {
                merge_dicts(
                    left_value.cast::<PyDict>().map_err(PyErr::from)?,
                    right_value.cast::<PyDict>().map_err(PyErr::from)?,
                    depth + 1,
                )?
            }
            _ => right_value.unbind(),
        };
        result.set_item(key, merged)?;
    }
    Ok(result.into_any().unbind())
}

fn deepcopy<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import("copy")?
        .getattr("deepcopy")?
        .call1((value,))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeSchema>()?;
    module.add_class::<AsyncSession>()?;
    module.add("BACKEND", "rust")?;
    module.add("MAX_DEPTH", MAX_DEPTH)?;
    Ok(())
}
