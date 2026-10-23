//! Async dataclass decode/encode continuations; mirrors the synchronous
//! `schema/evaluate/dataclass.rs` evaluation step for step.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple, PyType};

use super::super::{
    AsyncFrame, AsyncNext, AsyncSession, AsyncTask, DataclassDecodeFrame, DataclassDecodeStage,
    DataclassEncodeFrame, clone_path, deliver_at,
};
use super::child_path;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::{
    FLAG_HAS_DEFAULT, FLAG_HAS_DEFAULT_NODE, FLAG_IS_INITVAR, NativeSchema, assemble_instance,
    missing_default_error, resolve_default,
};

/// Field finished without a child evaluation; the caller advances the cursor.
enum DefaultStep {
    Done,
    // Evaluate the default-validation child with the resolved default.
    Eval(Py<PyAny>),
}

fn route_init_value(
    py: Python<'_>,
    frame: &mut DataclassDecodeFrame,
    index: usize,
    value: Py<PyAny>,
) -> PyResult<()> {
    if frame.flags[index] & FLAG_IS_INITVAR != 0 {
        frame.initvar_values.push(value);
    } else {
        let name = frame.names[index].clone_ref(py);
        frame.stored.bind(py).set_item(name.bind(py), value)?;
    }
    Ok(())
}

/// Omitted (or MISSING-producing) init field: append the required issue or
/// resolve the default, mirroring the synchronous default path.
fn begin_default(
    py: Python<'_>,
    ctx: &Context<'_>,
    frame: &mut DataclassDecodeFrame,
    index: usize,
) -> Result<DefaultStep> {
    let flag = frame.flags[index];
    if flag & FLAG_HAS_DEFAULT == 0 {
        let path = child_path(
            py,
            &frame.path,
            PathPart::Value(frame.input_names[index].clone_ref(py)),
        );
        frame
            .issues
            .push(Issue::simple(py, "required", "Required", &path));
        return Ok(DefaultStep::Done);
    }
    let name = frame.names[index].clone_ref(py);
    let resolved = {
        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
        let values = node.args[3]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let factories = node.args[4]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        resolve_default(ctx, values, factories, index, name.bind(py))?
    };
    if flag & FLAG_HAS_DEFAULT_NODE == 0 {
        route_init_value(py, frame, index, resolved)?;
        return Ok(DefaultStep::Done);
    }
    Ok(DefaultStep::Eval(resolved))
}

impl AsyncSession {
    #[allow(clippy::too_many_arguments)]
    pub(in crate::session) fn start_dataclass_decode(
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
        let build = (|| -> PyResult<DataclassDecodeFrame> {
            let names = node.args[1].bind(py).cast::<PyTuple>()?;
            let flags = node.args[2].bind(py).cast::<PyTuple>()?;
            let non_init_names = node.args[5].bind(py).cast::<PyTuple>()?;
            let input_names = node.args[9].bind(py).cast::<PyTuple>()?;
            // Fetch every field value before evaluating: a field callback can
            // mutate the input dict, so reads and the snapshot follow the
            // object node's fetch-then-copy order.
            let mut raws = Vec::with_capacity(names.len());
            let mut present = 0;
            for input_name in input_names.iter() {
                let raw = dict.get_item(input_name)?;
                if raw.is_some() {
                    present += 1;
                }
                raws.push(raw.map(Bound::unbind));
            }
            let extra_keys = if present == dict.len() {
                Vec::new()
            } else {
                dict.copy()?.iter().map(|(key, _)| key.unbind()).collect()
            };
            Ok(DataclassDecodeFrame {
                node: node_py,
                path: clone_path(py, &path),
                depth,
                names: names.iter().map(Bound::unbind).collect(),
                input_names: input_names.iter().map(Bound::unbind).collect(),
                flags: flags
                    .iter()
                    .map(|flag| flag.extract::<u8>())
                    .collect::<PyResult<Vec<_>>>()?,
                raws,
                non_init_names: non_init_names.iter().map(Bound::unbind).collect(),
                extra_keys,
                stored: PyDict::new(py).unbind(),
                initvar_values: Vec::new(),
                issues: Vec::new(),
                cursor: 0,
                stage: DataclassDecodeStage::Supplied,
            })
        })();
        match build {
            Ok(frame) => {
                self.frames.push(AsyncFrame::DataclassDecode(frame));
                self.advance(py)
            }
            Err(error) => Ok(deliver_at(Err(Error::Python(error)), path)),
        }
    }

    fn eval_decode_child(
        &mut self,
        py: Python<'_>,
        frame: DataclassDecodeFrame,
        child_index: usize,
        value: Py<PyAny>,
        path_key: Py<PyAny>,
    ) -> PyResult<AsyncNext> {
        let child = frame
            .node
            .bind(py)
            .try_borrow()
            .map_err(PyErr::from)?
            .children[child_index]
            .clone_ref(py);
        let path = child_path(py, &frame.path, PathPart::Value(path_key));
        let depth = frame.depth;
        self.frames.push(AsyncFrame::DataclassDecode(frame));
        Ok(AsyncNext::Task(AsyncTask::Eval {
            node: child,
            value,
            path,
            depth: depth + 1,
        }))
    }

    fn finish_dataclass_decode(
        &mut self,
        py: Python<'_>,
        mut frame: DataclassDecodeFrame,
    ) -> AsyncNext {
        for key in &frame.extra_keys {
            if !key.bind(py).is_instance_of::<PyString>() {
                let params = PyDict::new(py);
                if let Err(error) = params.set_item("expected", "string") {
                    return deliver_at(Err(Error::Python(error)), frame.path);
                }
                frame.issues.push(Issue::new(
                    py,
                    "invalid_key",
                    "Object keys must be strings",
                    &frame.path,
                    params.into_any(),
                ));
            }
        }
        if !frame.issues.is_empty() {
            return deliver_at(Err(Error::Invalid(frame.issues)), frame.path);
        }
        let assembled = (|| -> PyResult<Py<PyAny>> {
            let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
            let cls = node.args[0]
                .bind(py)
                .cast::<PyType>()
                .map_err(PyErr::from)?;
            let has_post_init: bool = node.args[6].bind(py).extract()?;
            assemble_instance(
                py,
                cls,
                frame.stored.bind(py),
                &frame.initvar_values,
                has_post_init,
            )
        })();
        match assembled {
            Ok(instance) => deliver_at(Ok(instance), frame.path),
            Err(error) => deliver_at(Err(Error::Python(error)), frame.path),
        }
    }

    pub(in crate::session) fn advance_dataclass_decode(
        &mut self,
        py: Python<'_>,
        mut frame: DataclassDecodeFrame,
    ) -> PyResult<AsyncNext> {
        let ctx = self.context(py)?;
        loop {
            match frame.stage {
                DataclassDecodeStage::Supplied => {
                    if frame.cursor >= frame.names.len() {
                        frame.stage = DataclassDecodeStage::NonInit;
                        frame.cursor = 0;
                        continue;
                    }
                    let index = frame.cursor;
                    if let Some(raw) = frame.raws[index].take() {
                        let key = frame.input_names[index].clone_ref(py);
                        return self.eval_decode_child(py, frame, 2 * index, raw, key);
                    }
                    match begin_default(py, &ctx, &mut frame, index) {
                        Ok(DefaultStep::Done) => frame.cursor += 1,
                        Ok(DefaultStep::Eval(resolved)) => {
                            frame.stage = DataclassDecodeStage::Default;
                            let key = frame.names[index].clone_ref(py);
                            return self.eval_decode_child(py, frame, 2 * index + 1, resolved, key);
                        }
                        Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                    }
                }
                DataclassDecodeStage::NonInit => {
                    if frame.cursor >= frame.non_init_names.len() {
                        return Ok(self.finish_dataclass_decode(py, frame));
                    }
                    let index = frame.cursor;
                    let name = frame.non_init_names[index].clone_ref(py);
                    let resolved = {
                        let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                        let values = node.args[7]
                            .bind(py)
                            .cast::<PyTuple>()
                            .map_err(PyErr::from)?;
                        let factories = node.args[8]
                            .bind(py)
                            .cast::<PyTuple>()
                            .map_err(PyErr::from)?;
                        resolve_default(&ctx, values, factories, index, name.bind(py))
                    };
                    match resolved {
                        Ok(resolved) => {
                            let child_index = 2 * frame.names.len() + index;
                            return self.eval_decode_child(py, frame, child_index, resolved, name);
                        }
                        Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                    }
                }
                DataclassDecodeStage::Default => {
                    return Err(PyRuntimeError::new_err(
                        "Dataclass decode frame cannot advance a pending default",
                    ));
                }
            }
        }
    }

    pub(in crate::session) fn resume_dataclass_decode(
        &mut self,
        py: Python<'_>,
        mut frame: DataclassDecodeFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let ctx = self.context(py)?;
        let index = frame.cursor;
        match frame.stage {
            DataclassDecodeStage::Supplied => match outcome {
                Ok(value) => {
                    if value.bind(py).is(&ctx.missing) {
                        match begin_default(py, &ctx, &mut frame, index) {
                            Ok(DefaultStep::Done) => frame.cursor += 1,
                            Ok(DefaultStep::Eval(resolved)) => {
                                frame.stage = DataclassDecodeStage::Default;
                                let key = frame.names[index].clone_ref(py);
                                return self.eval_decode_child(
                                    py,
                                    frame,
                                    2 * index + 1,
                                    resolved,
                                    key,
                                );
                            }
                            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                        }
                    } else {
                        if let Err(error) = route_init_value(py, &mut frame, index, value) {
                            return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                        }
                        frame.cursor += 1;
                    }
                }
                Err(Error::Invalid(mut issues)) => {
                    frame.issues.append(&mut issues);
                    frame.cursor += 1;
                }
                Err(error) => return Ok(deliver_at(Err(error), frame.path)),
            },
            DataclassDecodeStage::Default => {
                match outcome {
                    Ok(value) => {
                        if value.bind(py).is(&ctx.missing) {
                            let name = frame.names[index].clone_ref(py);
                            return Ok(deliver_at(
                                missing_default_error(name.bind(py), "default produced MISSING"),
                                frame.path,
                            ));
                        }
                        if let Err(error) = route_init_value(py, &mut frame, index, value) {
                            return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                        }
                    }
                    Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
                    Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                }
                frame.cursor += 1;
                frame.stage = DataclassDecodeStage::Supplied;
            }
            DataclassDecodeStage::NonInit => {
                match outcome {
                    Ok(value) => {
                        let name = frame.non_init_names[index].clone_ref(py);
                        if value.bind(py).is(&ctx.missing) {
                            return Ok(deliver_at(
                                missing_default_error(name.bind(py), "default produced MISSING"),
                                frame.path,
                            ));
                        }
                        if let Err(error) = frame.stored.bind(py).set_item(name.bind(py), value) {
                            return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                        }
                    }
                    Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
                    Err(error) => return Ok(deliver_at(Err(error), frame.path)),
                }
                frame.cursor += 1;
            }
        }
        self.frames.push(AsyncFrame::DataclassDecode(frame));
        self.advance(py)
    }

    #[allow(clippy::too_many_arguments)]
    pub(in crate::session) fn start_dataclass_encode(
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
        let cls = node.args[0].bind(py);
        let is_instance = match value.bind(py).is_instance(cls) {
            Ok(is_instance) => is_instance,
            Err(error) => return Ok(deliver_at(Err(Error::Python(error)), path)),
        };
        if !is_instance {
            let name: PyResult<String> = cls.getattr("__name__")?.extract();
            let name = match name {
                Ok(name) => name,
                Err(error) => return Ok(deliver_at(Err(Error::Python(error)), path)),
            };
            let params = PyDict::new(py);
            params.set_item("expected", &name)?;
            let outcome = ctx.fail_with("invalid_type", &format!("Expected {name}"), &path, params);
            return Ok(deliver_at(outcome, path));
        }
        let build = (|| -> PyResult<DataclassEncodeFrame> {
            let names = node.args[1].bind(py).cast::<PyTuple>()?;
            Ok(DataclassEncodeFrame {
                node: node_py,
                path: clone_path(py, &path),
                depth,
                value,
                names: names.iter().map(Bound::unbind).collect(),
                cursor: 0,
                result: PyDict::new(py).unbind(),
                issues: Vec::new(),
            })
        })();
        match build {
            Ok(frame) => {
                self.frames.push(AsyncFrame::DataclassEncode(frame));
                self.advance(py)
            }
            Err(error) => Ok(deliver_at(Err(Error::Python(error)), path)),
        }
    }

    pub(in crate::session) fn advance_dataclass_encode(
        &mut self,
        py: Python<'_>,
        frame: DataclassEncodeFrame,
    ) -> PyResult<AsyncNext> {
        if frame.cursor >= frame.names.len() {
            let outcome = if frame.issues.is_empty() {
                Ok(frame.result.clone_ref(py).into_any())
            } else {
                Err(Error::Invalid(frame.issues))
            };
            return Ok(deliver_at(outcome, frame.path));
        }
        let index = frame.cursor;
        let name = frame.names[index].clone_ref(py);
        let name_bound = name.bind(py);
        let raw = match name_bound.cast::<PyString>() {
            Ok(attr) => match frame.value.bind(py).getattr(attr) {
                Ok(raw) => raw.unbind(),
                Err(error) => return Ok(deliver_at(Err(Error::Python(error)), frame.path)),
            },
            Err(error) => return Ok(deliver_at(Err(Error::Python(error.into())), frame.path)),
        };
        let child = frame
            .node
            .bind(py)
            .try_borrow()
            .map_err(PyErr::from)?
            .children[index]
            .clone_ref(py);
        let path = child_path(py, &frame.path, PathPart::Value(name.clone_ref(py)));
        let depth = frame.depth;
        self.frames.push(AsyncFrame::DataclassEncode(frame));
        Ok(AsyncNext::Task(AsyncTask::Eval {
            node: child,
            value: raw,
            path,
            depth: depth + 1,
        }))
    }

    pub(in crate::session) fn resume_dataclass_encode(
        &mut self,
        py: Python<'_>,
        mut frame: DataclassEncodeFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match outcome {
            Ok(value) => {
                let node = frame.node.bind(py).try_borrow().map_err(PyErr::from)?;
                let name = node.args[2].bind(py).get_item(frame.cursor)?;
                if let Err(error) = frame.result.bind(py).set_item(name, value) {
                    return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                }
            }
            Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        }
        frame.cursor += 1;
        self.frames.push(AsyncFrame::DataclassEncode(frame));
        self.advance(py)
    }
}
