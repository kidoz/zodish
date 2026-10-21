//! Async container snapshots, traversal, result construction, and issue aggregation.
//! Object fields, array/set/tuple items, and record keys/values retain their
//! existing continuation order and depth accounting.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

use super::{
    ArrayFrame, AsyncFrame, AsyncNext, AsyncSession, AsyncTask, ObjectFrame, RecordFrame,
    RecordStage, clone_path, deliver_at,
};
use crate::MAX_DEPTH;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::{Kind, NativeSchema, UnknownKeys, is_primitive_kind};
use crate::values::{deepcopy, primitive_matches};

impl AsyncSession {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn start_object(
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
        let build = (|| -> PyResult<ObjectFrame> {
            let keys = node.args[0].bind(py).cast::<PyTuple>()?;
            let result = PyDict::new(py);
            let mut values = Vec::with_capacity(keys.len());
            let mut fast = Vec::with_capacity(keys.len());
            let mut present = 0;
            for (index, key) in keys.iter().enumerate() {
                let found = dict.get_item(&key)?;
                if let Some(raw) = found {
                    present += 1;
                    let child = node.children[index].try_borrow(py)?;
                    let is_fast = depth + 1 < MAX_DEPTH
                        && is_primitive_kind(child.kind)
                        && primitive_matches(&raw, child.kind)?;
                    values.push(Some(raw.unbind()));
                    fast.push(is_fast);
                } else {
                    values.push(Some(ctx.missing.clone().unbind()));
                    fast.push(false);
                }
            }
            let extras = if present == dict.len() {
                Vec::new()
            } else {
                let snapshot = dict.copy()?;
                snapshot
                    .iter()
                    .map(|(key, value)| (key.unbind(), value.unbind()))
                    .collect()
            };
            Ok(ObjectFrame {
                node: node_py,
                depth,
                keys: keys.iter().map(Bound::unbind).collect(),
                values,
                fast,
                cursor: 0,
                result: result.unbind(),
                issues: Vec::new(),
                extras,
                extra_cursor: 0,
                extras_phase: false,
                path: clone_path(py, &path),
            })
        })();
        match build {
            Ok(frame) => {
                self.frames.push(AsyncFrame::Object(frame));
                self.advance(py)
            }
            Err(error) => Ok(deliver_at(Err(Error::Python(error)), path)),
        }
    }

    pub(super) fn advance_object(
        &mut self,
        py: Python<'_>,
        mut frame: ObjectFrame,
    ) -> PyResult<AsyncNext> {
        let policy = frame
            .node
            .bind(py)
            .try_borrow()
            .map_err(PyErr::from)?
            .unknown_keys;
        let catchall = frame.node.bind(py).try_borrow().map_err(PyErr::from)?.args[2].clone_ref(py);
        loop {
            if !frame.extras_phase {
                while frame.cursor < frame.values.len() && frame.fast[frame.cursor] {
                    let index = frame.cursor;
                    let raw = frame.values[index].take().expect("fast object field");
                    if let Err(error) = frame
                        .result
                        .bind(py)
                        .set_item(frame.keys[index].bind(py), raw)
                    {
                        return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                    }
                    frame.cursor += 1;
                }
                if frame.cursor < frame.values.len() {
                    let index = frame.cursor;
                    let key = frame.keys[index].clone_ref(py);
                    let raw = frame.values[index].take().expect("non-fast object field");
                    frame.cursor += 1;
                    let child = frame
                        .node
                        .bind(py)
                        .try_borrow()
                        .map_err(PyErr::from)?
                        .children[index]
                        .clone_ref(py);
                    let child_path = child_path(py, &frame.path, PathPart::Value(key));
                    let depth = frame.depth;
                    self.frames.push(AsyncFrame::Object(frame));
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value: raw,
                        path: child_path,
                        depth: depth + 1,
                    }));
                }
                frame.extras_phase = true;
            }
            if frame.extra_cursor >= frame.extras.len() {
                let outcome = if frame.issues.is_empty() {
                    Ok(frame.result.clone_ref(py).into_any())
                } else {
                    Err(Error::Invalid(frame.issues))
                };
                return Ok(deliver_at(outcome, frame.path));
            }
            let index = frame.extra_cursor;
            frame.extra_cursor += 1;
            let (key, raw) = &frame.extras[index];
            let key = key.clone_ref(py);
            let raw = raw.clone_ref(py);
            let key_bound = key.bind(py);
            if !key_bound.is_instance_of::<PyString>() {
                let params = PyDict::new(py);
                params.set_item("expected", "string")?;
                frame.issues.push(Issue::new(
                    py,
                    "invalid_key",
                    "Object keys must be strings",
                    &frame.path,
                    params.into_any(),
                ));
                continue;
            }
            let mut known = false;
            for candidate in &frame.keys {
                if key_bound.eq(candidate.bind(py))? {
                    known = true;
                    break;
                }
            }
            if known {
                continue;
            }
            match policy {
                UnknownKeys::Strict => {
                    let params = PyDict::new(py);
                    params.set_item("keys", PyTuple::new(py, vec![key.clone_ref(py)])?)?;
                    let issue_path =
                        child_path(py, &frame.path, PathPart::Value(key.clone_ref(py)));
                    frame.issues.push(Issue::new(
                        py,
                        "unrecognized_key",
                        "Unrecognized key",
                        &issue_path,
                        params.into_any(),
                    ));
                }
                UnknownKeys::Passthrough => {
                    let value = match deepcopy(raw.bind(py)) {
                        Ok(value) => value,
                        Err(error) => {
                            return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                        }
                    };
                    if let Err(error) = frame.result.bind(py).set_item(&key, value) {
                        return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                    }
                }
                UnknownKeys::Catchall => {
                    let catchall_node = match catchall.bind(py).extract::<Py<NativeSchema>>() {
                        Ok(node) => node,
                        Err(error) => {
                            return Ok(deliver_at(Err(Error::Python(error.into())), frame.path));
                        }
                    };
                    let child_path =
                        child_path(py, &frame.path, PathPart::Value(key.clone_ref(py)));
                    let depth = frame.depth;
                    self.frames.push(AsyncFrame::Object(frame));
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: catchall_node,
                        value: raw,
                        path: child_path,
                        depth: depth + 1,
                    }));
                }
                UnknownKeys::Strip => {}
            }
        }
    }

    pub(super) fn resume_object(
        &mut self,
        py: Python<'_>,
        mut frame: ObjectFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        let extra_child = frame.extras_phase;
        let index = if extra_child {
            frame.extra_cursor.saturating_sub(1)
        } else {
            frame.cursor.saturating_sub(1)
        };
        match outcome {
            Ok(value) => {
                let missing = self
                    .missing
                    .as_ref()
                    .ok_or_else(|| PyRuntimeError::new_err("Async session is closed"))?
                    .bind(py);
                let should_set = extra_child || !value.bind(py).is(missing);
                if should_set {
                    let key = if extra_child {
                        frame.extras[index].0.clone_ref(py)
                    } else {
                        frame.keys[index].clone_ref(py)
                    };
                    if let Err(error) = frame.result.bind(py).set_item(key.bind(py), value) {
                        return Ok(deliver_at(Err(Error::Python(error)), frame.path));
                    }
                }
            }
            Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        }
        self.frames.push(AsyncFrame::Object(frame));
        self.advance(py)
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn start_array(
        &mut self,
        py: Python<'_>,
        ctx: &Context<'_>,
        node_py: Py<NativeSchema>,
        node: &NativeSchema,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        let is_tuple = matches!(node.kind, Kind::Tuple);
        let is_set = matches!(node.kind, Kind::Set);
        let bound = value.bind(py);
        if bound.is(&ctx.missing) {
            let outcome = ctx.fail("required", "Required", &path);
            return Ok(deliver_at(outcome, path));
        }
        if is_set {
            if !bound.is_instance_of::<PySet>() {
                let params = PyDict::new(py);
                params.set_item("expected", "set")?;
                let outcome = ctx.fail_with("invalid_type", "Expected set", &path, params);
                return Ok(deliver_at(outcome, path));
            }
        } else if !bound.is_instance_of::<PyList>()
            && !(is_tuple && bound.is_instance_of::<PyTuple>())
        {
            let expected = if is_tuple { "tuple or list" } else { "list" };
            let params = PyDict::new(py);
            params.set_item("expected", expected)?;
            let outcome = ctx.fail_with(
                "invalid_type",
                &format!("Expected {expected}"),
                &path,
                params,
            );
            return Ok(deliver_at(outcome, path));
        }
        let items = (|| -> PyResult<Vec<Py<PyAny>>> {
            let mut items = Vec::new();
            if is_set {
                for item in bound.try_iter()? {
                    items.push(item?.unbind());
                }
            } else if bound.is_exact_instance_of::<PyList>() {
                for item in bound.cast::<PyList>()?.iter() {
                    items.push(item.unbind());
                }
            } else if is_tuple && bound.is_exact_instance_of::<PyTuple>() {
                for item in bound.cast::<PyTuple>()?.iter() {
                    items.push(item.unbind());
                }
            } else {
                for item in bound.try_iter()? {
                    items.push(item?.unbind());
                }
            }
            Ok(items)
        })();
        let items = match items {
            Ok(items) => items,
            Err(error) => return Ok(deliver_at(Err(Error::Python(error)), path)),
        };
        let fixed = node.children.len();
        let rest = if is_tuple {
            let rest = node.args[0].bind(py);
            if rest.is_none() {
                if items.len() != fixed {
                    let params = PyDict::new(py);
                    params.set_item("expected", fixed)?;
                    let outcome = ctx.fail_with(
                        "invalid_length",
                        &format!("Expected {fixed} items"),
                        &path,
                        params,
                    );
                    return Ok(deliver_at(outcome, path));
                }
                None
            } else {
                if items.len() < fixed {
                    let params = PyDict::new(py);
                    params.set_item("minimum", fixed)?;
                    params.set_item("inclusive", true)?;
                    params.set_item("origin", "array")?;
                    let outcome = ctx.fail_with(
                        "too_small",
                        &format!("Expected at least {fixed} items"),
                        &path,
                        params,
                    );
                    return Ok(deliver_at(outcome, path));
                }
                match rest.extract::<Py<NativeSchema>>() {
                    Ok(rest) => Some(rest),
                    Err(error) => {
                        return Ok(deliver_at(Err(Error::Python(error.into())), path));
                    }
                }
            }
        } else {
            None
        };
        self.frames.push(AsyncFrame::Array(ArrayFrame {
            node: node_py,
            path,
            depth,
            items,
            index: 0,
            result: Vec::new(),
            issues: Vec::new(),
            is_tuple,
            is_set,
            fixed,
            rest,
        }));
        self.advance(py)
    }

    pub(super) fn advance_array(
        &mut self,
        py: Python<'_>,
        mut frame: ArrayFrame,
    ) -> PyResult<AsyncNext> {
        if frame.index >= frame.items.len() {
            if !frame.issues.is_empty() {
                return Ok(deliver_at(Err(Error::Invalid(frame.issues)), frame.path));
            }
            let outcome = if frame.is_set {
                let set = PySet::empty(py)?;
                for value in frame.result {
                    set.add(value.bind(py))?;
                }
                Ok(set.into_any().unbind())
            } else if frame.is_tuple {
                Ok(PyTuple::new(py, frame.result)?.into_any().unbind())
            } else {
                Ok(PyList::new(py, frame.result)?.into_any().unbind())
            };
            return Ok(deliver_at(outcome, frame.path));
        }
        let index = frame.index;
        frame.index += 1;
        let value = frame.items[index].clone_ref(py);
        let child = if frame.is_tuple && index >= frame.fixed {
            frame
                .rest
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("Async tuple has no rest schema"))?
                .clone_ref(py)
        } else {
            frame
                .node
                .bind(py)
                .try_borrow()
                .map_err(PyErr::from)?
                .children[if frame.is_tuple { index } else { 0 }]
            .clone_ref(py)
        };
        let path = child_path(py, &frame.path, PathPart::Index(index));
        let depth = frame.depth;
        self.frames.push(AsyncFrame::Array(frame));
        Ok(AsyncNext::Task(AsyncTask::Eval {
            node: child,
            value,
            path,
            depth: depth + 1,
        }))
    }

    pub(super) fn resume_array(
        &mut self,
        py: Python<'_>,
        mut frame: ArrayFrame,
        outcome: Result<Py<PyAny>>,
    ) -> PyResult<AsyncNext> {
        match outcome {
            Ok(value) => frame.result.push(value),
            Err(Error::Invalid(mut issues)) => frame.issues.append(&mut issues),
            Err(error) => return Ok(deliver_at(Err(error), frame.path)),
        }
        self.frames.push(AsyncFrame::Array(frame));
        self.advance(py)
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn start_record(
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

    pub(super) fn advance_record(
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

    pub(super) fn resume_record(
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

fn child_path(py: Python<'_>, path: &Path, part: PathPart) -> Path {
    let mut child = clone_path(py, path);
    child.push(part);
    child
}
