//! Async execution loop, outcome delivery, and frame routing.
//! Node evaluation lives in `evaluate`; continuation bodies live in the sibling
//! container, composition, callback, and wrapper modules.

use pyo3::exceptions::{PyException, PyRuntimeError};
use pyo3::prelude::*;

use super::{AsyncFrame, AsyncNext, AsyncSession, AsyncTask};
use crate::errors::{Error, Result};

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
    fn resume_frame(
        &mut self,
        py: Python<'_>,
        frame: AsyncFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match frame {
            AsyncFrame::AsyncCallback(frame) => self.resume_async_callback(py, frame, outcome),
            AsyncFrame::Catch(frame) => self.resume_catch(py, frame, outcome),
            AsyncFrame::Refine(frame) => self.resume_refine(py, frame, outcome),
            AsyncFrame::SuperRefine(frame) => self.resume_super_refine(py, frame, outcome),
            AsyncFrame::PostChild(frame) => self.resume_post_child(py, frame, outcome),
            AsyncFrame::OutputGuard(frame) => self.resume_output_guard(py, frame, outcome),
            AsyncFrame::StringBool(frame) => self.resume_string_bool(py, frame, outcome),
            AsyncFrame::Pipe(frame) => self.resume_pipe(py, frame, outcome),
            AsyncFrame::Array(frame) => self.resume_array(py, frame, outcome),
            AsyncFrame::Object(frame) => self.resume_object(py, frame, outcome),
            AsyncFrame::Record(frame) => self.resume_record(py, frame, outcome),
            AsyncFrame::DataclassDecode(frame) => self.resume_dataclass_decode(py, frame, outcome),
            AsyncFrame::DataclassEncode(frame) => self.resume_dataclass_encode(py, frame, outcome),
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
            AsyncFrame::DataclassDecode(frame) => self.advance_dataclass_decode(py, frame),
            AsyncFrame::DataclassEncode(frame) => self.advance_dataclass_encode(py, frame),
            AsyncFrame::Union(frame) => self.advance_union(py, frame),
            AsyncFrame::Intersection(frame) => self.advance_intersection(py, frame),
            _ => Err(PyRuntimeError::new_err(
                "Async frame cannot be advanced without a child result",
            )),
        }
    }
}
