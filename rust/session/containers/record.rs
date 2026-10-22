//! Async record snapshots, key/value traversal, and issue aggregation.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

use super::super::{
    AsyncFrame, AsyncNext, AsyncSession, AsyncTask, RecordFrame, RecordStage, clone_path,
    deliver_at,
};
use super::child_path;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::NativeSchema;

impl AsyncSession {
    #[allow(clippy::too_many_arguments)]
    pub(in crate::session) fn start_record(
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

    pub(in crate::session) fn advance_record(
        &mut self,
        py: Python<'_>,
        mut frame: RecordFrame,
    ) -> PyResult<AsyncNext> {
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

    pub(in crate::session) fn resume_record(
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
}
