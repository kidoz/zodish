//! Rust-owned resumable validation sessions and their Python lifecycle.
//! Frames retain snapshots, paths, issues, and callbacks across suspension.
//! Execution and frame routing live in `dispatch`; node evaluation lives in
//! `evaluate`. Synchronous subtrees reuse the schema evaluator and shared checks.
//! Container continuations live
//! in `containers`, union/intersection continuations in `composition`; frame
//! storage and Python lifecycle methods stay here. Callback resumption and
//! awaitability checks live in `callbacks`; wrapper continuations in `wrappers`;
//! frame GC traversal lives in `gc`.

mod callbacks;
mod composition;
mod containers;
mod dispatch;
mod evaluate;
mod gc;
mod wrappers;

use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex};

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyException, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet, PyTraceback};

use crate::errors::{Error, Issue, Path, Result};
use crate::runtime::{Context, LazyHandle, LazyState};
use crate::schema::NativeSchema;

use gc::visit_async_frame;

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
    ContextRefine,
    ContextSuperRefine,
    ContextTransform,
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

struct OutputGuardFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
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
    encode_set: bool,
    fixed: usize,
    rest: Option<Py<NativeSchema>>,
}

struct ObjectFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    keys: Vec<Py<PyAny>>,
    input_keys: Vec<Py<PyAny>>,
    path_keys: Vec<Py<PyAny>>,
    output_names: Option<Py<PySet>>,
    values: Vec<Option<Py<PyAny>>>,
    duplicates: Vec<Option<Py<PyAny>>>,
    fast: Vec<bool>,
    cursor: usize,
    result: Py<PyDict>,
    issues: Vec<Issue>,
    extras: Vec<(Py<PyAny>, Py<PyAny>)>,
    extra_cursor: usize,
    extras_phase: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DataclassDecodeStage {
    // Evaluating the supplied-value child of the init field at `cursor`.
    Supplied,
    // Evaluating the default-validation child of the init field at `cursor`.
    Default,
    // Evaluating the validation child of the init=False field at `cursor`.
    NonInit,
}

struct DataclassDecodeFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    names: Vec<Py<PyAny>>,
    input_names: Vec<Py<PyAny>>,
    flags: Vec<u8>,
    raws: Vec<Option<Py<PyAny>>>,
    duplicates: Vec<Option<Py<PyAny>>>,
    non_init_names: Vec<Py<PyAny>>,
    // Snapshot keys retained for the post-field invalid_key scan; unknown
    // string keys are stripped, so values are never needed.
    extra_keys: Vec<Py<PyAny>>,
    stored: Py<PyDict>,
    initvar_values: Vec<Py<PyAny>>,
    issues: Vec<Issue>,
    cursor: usize,
    stage: DataclassDecodeStage,
}

struct DataclassEncodeFrame {
    node: Py<NativeSchema>,
    path: Path,
    depth: usize,
    value: Py<PyAny>,
    names: Vec<Py<PyAny>>,
    cursor: usize,
    result: Py<PyDict>,
    issues: Vec<Issue>,
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
    OutputGuard(OutputGuardFrame),
    StringBool(StringBoolFrame),
    Pipe(PipeFrame),
    Array(ArrayFrame),
    Object(ObjectFrame),
    Record(RecordFrame),
    DataclassDecode(DataclassDecodeFrame),
    DataclassEncode(DataclassEncodeFrame),
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
    callback_context: Option<Py<PyAny>>,
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
    #[pyo3(signature = (node, value, missing, issue_type, error_type, context=None))]
    fn new(
        node: Py<NativeSchema>,
        value: Py<PyAny>,
        missing: Py<PyAny>,
        issue_type: Py<PyAny>,
        error_type: Py<PyAny>,
        context: Option<Py<PyAny>>,
    ) -> Self {
        Self {
            node: Some(node),
            value: Some(value),
            missing: Some(missing),
            issue_type: Some(issue_type),
            error_type: Some(error_type),
            callback_context: context,
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
        let has_async = match node.bind(py).try_borrow() {
            Ok(node) => node.has_async,
            Err(error) => return self.close_on_error(py, PyErr::from(error)),
        };
        if !has_async {
            let parsed = node.bind(py).borrow().parse(
                py,
                value.bind(py).clone(),
                missing.bind(py).clone(),
                issue_type.bind(py).clone(),
                error_type.bind(py).clone(),
                self.callback_context
                    .as_ref()
                    .map(|context| context.bind(py).clone()),
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
        let pending = match self.pending.take() {
            Some(pending) => pending,
            None => {
                return self.close_on_error(
                    py,
                    PyRuntimeError::new_err("Async session has no pending callback"),
                );
            }
        };
        self.awaitable = None;
        self.state = AsyncSessionState::Running;
        let next = match self.resume_callback(py, pending, value) {
            Ok(next) => next,
            Err(error) => return self.close_on_error(py, error),
        };
        self.continue_run(py, next)
    }

    fn resume_error(
        &mut self,
        py: Python<'_>,
        token: u64,
        error: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        self.check_awaiting(token)?;
        let pending = match self.pending.take() {
            Some(pending) => pending,
            None => {
                return self.close_on_error(
                    py,
                    PyRuntimeError::new_err("Async session has no pending callback"),
                );
            }
        };
        self.awaitable = None;
        let exception = error.bind(py);
        if !exception.is_instance_of::<PyException>() {
            return self.close_on_error(py, PyErr::from_value(exception.clone()));
        }
        self.state = AsyncSessionState::Running;
        let ctx = match self.context(py) {
            Ok(ctx) => ctx,
            Err(error) => return self.close_on_error(py, error),
        };
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
            &self.callback_context,
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
            callback_context: self
                .callback_context
                .as_ref()
                .map(|context| context.bind(py).clone())
                .unwrap_or_else(|| py.None().bind(py).clone()),
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
        self.callback_context = None;
        self.frames.clear();
        self.pending = None;
        self.awaitable = None;
        self.lazy_store.lock().unwrap().cache.clear();
        self.lazy_store.lock().unwrap().last = None;
        self.thread = None;
    }

    fn continue_run(&mut self, py: Python<'_>, next: AsyncNext) -> PyResult<Py<PyAny>> {
        let next = match next {
            AsyncNext::Task(task) => match self.run(py, task) {
                Ok(next) => next,
                Err(error) => return self.close_on_error(py, error),
            },
            other => other,
        };
        match self.finish(py, next) {
            Ok(value) => Ok(value),
            Err(error) => self.close_on_error(py, error),
        }
    }

    fn close_on_error(&mut self, py: Python<'_>, error: PyErr) -> PyResult<Py<PyAny>> {
        self.close(py);
        Err(error)
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
