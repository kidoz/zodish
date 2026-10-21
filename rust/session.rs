//! Rust-owned resumable validation sessions and their Python lifecycle.
//! Frames retain snapshots, paths, issues, and callbacks across suspension.
//! Execution and node/frame dispatch live in `dispatch`; synchronous subtrees
//! reuse the schema evaluator and shared checks. Container continuations live
//! in `containers`; frame storage and Python lifecycle methods stay here.

mod containers;
mod dispatch;

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyException, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTraceback, PyTuple};

use crate::errors::{Error, Issue, Path, PathPart, Result, visit_issue};
use crate::runtime::{Context, LazyHandle, LazyState};
use crate::schema::{Kind, NativeSchema};
use crate::values::merge_dicts;

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
