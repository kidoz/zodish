//! Rust-owned resumable validation sessions and their Python lifecycle.
//! Frames retain snapshots, paths, issues, and callbacks across suspension.
//! Execution and node/frame dispatch live in `dispatch`; synchronous subtrees
//! reuse the schema evaluator and shared checks.

mod dispatch;

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyException, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTraceback, PyTuple};

use crate::MAX_DEPTH;
use crate::errors::{Error, Issue, Path, PathPart, Result, visit_issue};
use crate::runtime::{Context, LazyHandle, LazyState};
use crate::schema::{Kind, NativeSchema, UnknownKeys, is_primitive_kind};
use crate::values::{deepcopy, merge_dicts, primitive_matches};

static ASYNC_TOKEN_COUNTER: AtomicU64 = AtomicU64::new(1);

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
    is_set: bool,
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
pub(crate) struct AsyncSession {
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
        let is_set = matches!(node.kind, Kind::Set);
        let bound = value.bind(py);
        if bound.is(&ctx.missing) {
            let outcome = ctx.fail("required", "Required", &path);
            return Ok(deliver_at(outcome, path));
        }
        if is_set {
            if !bound.is_instance_of::<PySet>() {
                let params = PyDict::new(py);
                params.set_item("expected", "set")?;
                let outcome = ctx.fail_with("invalid_type", "Expected set", &path, params);
                return Ok(deliver_at(outcome, path));
            }
        } else if !bound.is_instance_of::<PyList>()
            && !(is_tuple && bound.is_instance_of::<PyTuple>())
        {
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
            if is_set {
                for item in bound.try_iter()? {
                    items.push(item?.unbind());
                }
            } else if bound.is_exact_instance_of::<PyList>() {
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
            is_set,
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
            let outcome = if frame.is_set {
                let set = PySet::empty(py)?;
                for value in frame.result {
                    set.add(value.bind(py))?;
                }
                Ok(set.into_any().unbind())
            } else if frame.is_tuple {
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
