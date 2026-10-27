//! Wrapper continuations for resumable async validation sessions.
//! Frame storage, token ownership, and Python lifecycle stay in the parent session.

use std::sync::atomic::Ordering;

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;

use super::callbacks::check_awaitable;
use super::{
    ASYNC_TOKEN_COUNTER, AsyncFrame, AsyncNext, AsyncPending, AsyncPendingKind, AsyncSession,
    AsyncTask, CatchFrame, OutputGuardFrame, PipeFrame, PostChildFrame, RefineFrame,
    StringBoolFrame, SuperRefineFrame, clone_path, deliver_at,
};
use crate::errors::{Error, PathPart, Result};
use crate::schema::{evaluate_string_bool, interpret_super_refine};
use crate::values::deepcopy;

impl AsyncSession {
    pub(super) fn resume_catch(
        &mut self,
        py: Python<'_>,
        frame: CatchFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
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

    pub(super) fn resume_refine(
        &mut self,
        py: Python<'_>,
        frame: RefineFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let path = frame.path;
        match outcome {
            Ok(parsed) => {
                let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                let result = (|| -> Result<Py<PyAny>> {
                    let parsed_bound = parsed.bind(py);
                    let ctx = self.context(py)?;
                    let accepted = if matches!(node.kind, crate::schema::Kind::ContextRefine) {
                        node.args[0]
                            .bind(py)
                            .call1((parsed_bound, &ctx.callback_context))?
                            .is_truthy()?
                    } else {
                        node.args[0].bind(py).call1((parsed_bound,))?.is_truthy()?
                    };
                    if accepted {
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

    pub(super) fn resume_super_refine(
        &mut self,
        py: Python<'_>,
        frame: SuperRefineFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let path = frame.path;
        match outcome {
            Ok(parsed) => {
                let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                let result = (|| -> Result<Py<PyAny>> {
                    let ctx = self.context(py)?;
                    let result = if matches!(node.kind, crate::schema::Kind::ContextSuperRefine) {
                        node.args[0]
                            .bind(py)
                            .call1((parsed.bind(py), &ctx.callback_context))?
                    } else {
                        node.args[0].bind(py).call1((parsed.bind(py),))?
                    };
                    interpret_super_refine(&ctx, parsed, &result, &path, "super_refine")
                })();
                drop(node);
                Ok(deliver_at(result, path))
            }
            Err(error) => Ok(deliver_at(Err(error), path)),
        }
    }

    pub(super) fn resume_post_child(
        &mut self,
        py: Python<'_>,
        frame: PostChildFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
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

    pub(super) fn resume_output_guard(
        &mut self,
        py: Python<'_>,
        frame: OutputGuardFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let path = frame.path;
        match outcome {
            Ok(parsed) => {
                let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                let result = (|| -> Result<Py<PyAny>> {
                    let guard = node.children[1].try_borrow(py).map_err(PyErr::from)?;
                    let ctx = self.context(py)?;
                    let mut guard_path = clone_path(py, &path);
                    match guard.validate(&ctx, parsed.bind(py), &mut guard_path, frame.depth + 1) {
                        Ok(_) => Ok(parsed.bind(py).clone().unbind()),
                        Err(Error::Invalid(_)) => {
                            let field: String = node.args[0].bind(py).extract()?;
                            let expected: String = node.args[1].bind(py).extract()?;
                            let actual = parsed.bind(py).get_type().name()?.to_string();
                            Err(PyTypeError::new_err(format!(
                                "field {field:?} produced {actual}, expected {expected}"
                            ))
                            .into())
                        }
                        Err(error) => Err(error),
                    }
                })();
                drop(node);
                Ok(deliver_at(result, path))
            }
            Err(error) => Ok(deliver_at(Err(error), path)),
        }
    }

    pub(super) fn resume_string_bool(
        &mut self,
        py: Python<'_>,
        frame: StringBoolFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let path = frame.path;
        match outcome {
            Ok(parsed) => {
                let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                let ctx = self.context(py)?;
                let result = evaluate_string_bool(&ctx, &node, parsed.bind(py), &path);
                drop(node);
                Ok(deliver_at(result, path))
            }
            Err(error) => Ok(deliver_at(Err(error), path)),
        }
    }

    pub(super) fn resume_pipe(
        &mut self,
        py: Python<'_>,
        frame: PipeFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
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
}
