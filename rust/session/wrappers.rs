//! Wrapper continuations for resumable async validation sessions.
//! Frame storage, token ownership, and Python lifecycle stay in the parent session.

use std::sync::atomic::Ordering;

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::callbacks::check_awaitable;
use super::{
    ASYNC_TOKEN_COUNTER, AsyncFrame, AsyncNext, AsyncPending, AsyncPendingKind, AsyncSession,
    AsyncTask, CatchFrame, PipeFrame, PostChildFrame, RefineFrame, StringBoolFrame,
    SuperRefineFrame, clone_path, deliver_at,
};
use crate::errors::{Error, Issue, PathPart, Result};
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
                            return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
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
                            return Ok(false.into_pyobject(py)?.to_owned().into_any().unbind());
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
