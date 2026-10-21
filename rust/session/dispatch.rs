//! Async execution loop, node evaluation, and frame dispatch.
//! Synchronous subtrees reuse the ordinary native evaluator; continuation
//! bodies retain their existing callback, issue, and depth behavior.

use std::sync::atomic::Ordering;

use pyo3::exceptions::{PyException, PyRecursionError, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::callbacks::check_awaitable;
use super::{
    ASYNC_TOKEN_COUNTER, AsyncCallbackFrame, AsyncFrame, AsyncNext, AsyncPending, AsyncPendingKind,
    AsyncSession, AsyncTask, CatchFrame, PipeFrame, PostChildFrame, RefineFrame, StringBoolFrame,
    SuperRefineFrame, clone_path, deliver_at,
};
use crate::MAX_DEPTH;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::LazyStore;
use crate::schema::{Kind, NativeSchema};
use crate::values::{coerce_value, deepcopy, enum_message};

impl AsyncSession {
    pub(super) fn run(&mut self, py: Python<'_>, mut task: AsyncTask) -> PyResult<AsyncNext> {
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
            | Kind::RequiredOutput
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
            Kind::Array | Kind::Set | Kind::Tuple => {
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

    pub(super) fn advance(&mut self, py: Python<'_>) -> PyResult<AsyncNext> {
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
