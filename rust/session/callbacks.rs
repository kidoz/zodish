//! Async callback resumption, awaitability checks, and result interpretation.
//! Pending request storage and the token counter remain owned by the session.

use std::sync::atomic::Ordering;

use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::{
    ASYNC_TOKEN_COUNTER, AsyncCallbackFrame, AsyncNext, AsyncPending, AsyncPendingKind,
    AsyncSession, AsyncTask, clone_path, deliver_at,
};
use crate::errors::{Error, Issue, PathPart, Result};
use crate::schema::{Kind, interpret_super_refine};

impl AsyncSession {
    pub(super) fn resume_async_callback(
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
            Kind::AsyncContextRefine => AsyncPendingKind::ContextRefine,
            Kind::AsyncContextSuperRefine => AsyncPendingKind::ContextSuperRefine,
            Kind::AsyncContextTransform => AsyncPendingKind::ContextTransform,
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
        let result = if matches!(
            pending_kind,
            AsyncPendingKind::ContextRefine
                | AsyncPendingKind::ContextSuperRefine
                | AsyncPendingKind::ContextTransform
        ) {
            let ctx = self.context(py)?;
            callback
                .bind(py)
                .call1((parsed.bind(py), &ctx.callback_context))
        } else {
            callback.bind(py).call1((parsed.bind(py),))
        };
        let result = match result {
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

    pub(super) fn resume_callback(
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
            AsyncPendingKind::Refine | AsyncPendingKind::ContextRefine => {
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
            AsyncPendingKind::SuperRefine | AsyncPendingKind::ContextSuperRefine => {
                let parsed =
                    parsed.ok_or_else(|| PyRuntimeError::new_err("Async session has no result"))?;
                let ctx = self.context(py)?;
                let outcome = interpret_super_refine(
                    &ctx,
                    parsed,
                    value.bind(py),
                    &path,
                    "super_refine_async",
                );
                Ok(deliver_at(outcome, path))
            }
            AsyncPendingKind::Transform
            | AsyncPendingKind::ContextTransform
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

pub(super) fn check_awaitable(py: Python<'_>, result: &Bound<'_, PyAny>) -> Result<Py<PyAny>> {
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
