//! Async union and intersection continuations and outcome combination.
//! Branch order, stored Python exceptions/tracebacks, and dictionary merging
//! retain their existing behavior across suspension.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::{
    AsyncFrame, AsyncNext, AsyncSession, AsyncTask, IntersectionFrame, StoredOutcome,
    StoredPythonError, UnionFrame, clone_path, deliver_at,
};
use crate::errors::{Error, Issue, Path, Result};
use crate::runtime::Context;
use crate::schema::NativeSchema;
use crate::values::merge_dicts;

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

impl AsyncSession {
    pub(super) fn start_union(
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

    pub(super) fn advance_union(
        &mut self,
        py: Python<'_>,
        mut frame: UnionFrame,
    ) -> PyResult<AsyncNext> {
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

    pub(super) fn resume_union(
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
    pub(super) fn start_intersection(
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

    pub(super) fn advance_intersection(
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

    pub(super) fn resume_intersection(
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
}
