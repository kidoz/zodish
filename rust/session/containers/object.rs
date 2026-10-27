//! Async object snapshots, field/extras traversal, fast paths, and issue aggregation.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet, PyString, PyTuple};

use super::super::{
    AsyncFrame, AsyncNext, AsyncSession, AsyncTask, ObjectFrame, clone_path, deliver_at,
};
use super::child_path;
use crate::MAX_DEPTH;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::{Kind, NativeSchema, UnknownKeys, is_primitive_kind};
use crate::values::{deepcopy, primitive_matches};

impl AsyncSession {
    #[allow(clippy::too_many_arguments)]
    pub(in crate::session) fn start_object(
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
            let input_keys = node.args[3].bind(py).cast::<PyTuple>()?;
            let input_aliases = node.args[4].bind(py).cast::<PyTuple>()?;
            let output_names = if matches!(
                node.unknown_keys,
                UnknownKeys::Passthrough | UnknownKeys::Catchall,
            ) {
                Some(PySet::new(py, keys.iter())?.unbind())
            } else {
                None
            };
            let result = PyDict::new(py);
            let mut values = Vec::with_capacity(keys.len());
            let mut duplicates = Vec::with_capacity(keys.len());
            let mut fast = Vec::with_capacity(keys.len());
            let mut path_keys = Vec::with_capacity(keys.len());
            let mut known_keys = Vec::new();
            let mut present = 0;
            for (index, aliases) in input_aliases.iter().enumerate() {
                let aliases = aliases.cast::<PyTuple>()?;
                let input_key = input_keys.get_item(index)?;
                let child = node.children[index].try_borrow(py)?;
                path_keys.push(if matches!(child.kind, Kind::RequiredOutput) {
                    keys.get_item(index)?.unbind()
                } else {
                    input_key.unbind()
                });
                let mut raw = None;
                let mut supplied = Vec::new();
                for alias in aliases.iter() {
                    known_keys.push(alias.clone().unbind());
                    if let Some(found) = dict.get_item(&alias)? {
                        present += 1;
                        supplied.push(alias.clone());
                        if raw.is_none() {
                            raw = Some(found);
                        }
                    }
                }
                let duplicate = if supplied.len() > 1 {
                    Some(PyTuple::new(py, supplied)?.into_any().unbind())
                } else {
                    None
                };
                let is_fast = duplicate.is_none()
                    && raw.is_some()
                    && depth + 1 < MAX_DEPTH
                    && is_primitive_kind(child.kind)
                    && primitive_matches(raw.as_ref().expect("present value"), child.kind)?;
                values.push(Some(match raw {
                    Some(raw) => raw.unbind(),
                    None => ctx.missing.clone().unbind(),
                }));
                duplicates.push(duplicate);
                fast.push(is_fast);
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
                input_keys: known_keys,
                path_keys,
                output_names,
                values,
                duplicates,
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

    pub(in crate::session) fn advance_object(
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
                    let path_key = frame.path_keys[index].clone_ref(py);
                    frame.cursor += 1;
                    if let Some(keys) = &frame.duplicates[index] {
                        frame.values[index].take();
                        let issue_path = child_path(py, &frame.path, PathPart::Value(path_key));
                        frame
                            .issues
                            .push(Issue::duplicate_key(py, &issue_path, keys.bind(py))?);
                        continue;
                    }
                    let raw = frame.values[index].take().expect("non-fast object field");
                    let child = frame
                        .node
                        .bind(py)
                        .try_borrow()
                        .map_err(PyErr::from)?
                        .children[index]
                        .clone_ref(py);
                    let child_path = child_path(py, &frame.path, PathPart::Value(path_key));
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
            for candidate in &frame.input_keys {
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
                    let collides = match &frame.output_names {
                        Some(names) => names.bind(py).contains(&key)?,
                        None => false,
                    };
                    if collides {
                        continue;
                    }
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
                    let collides = match &frame.output_names {
                        Some(names) => names.bind(py).contains(&key)?,
                        None => false,
                    };
                    if collides {
                        continue;
                    }
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

    pub(in crate::session) fn resume_object(
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
}
