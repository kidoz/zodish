//! Async array/set/tuple snapshots, item traversal, and result construction.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyTuple};

use super::super::{ArrayFrame, AsyncFrame, AsyncNext, AsyncSession, AsyncTask, deliver_at};
use super::child_path;
use crate::errors::{Error, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::{Kind, NativeSchema};

impl AsyncSession {
    #[allow(clippy::too_many_arguments)]
    pub(in crate::session) fn start_array(
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
        let is_set = matches!(node.kind, Kind::Set | Kind::SetEncode);
        let encode_set = matches!(node.kind, Kind::SetEncode);
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
            encode_set,
            fixed,
            rest,
        }));
        self.advance(py)
    }

    pub(in crate::session) fn advance_array(
        &mut self,
        py: Python<'_>,
        mut frame: ArrayFrame,
    ) -> PyResult<AsyncNext> {
        if frame.index >= frame.items.len() {
            if !frame.issues.is_empty() {
                return Ok(deliver_at(Err(Error::Invalid(frame.issues)), frame.path));
            }
            let outcome = if frame.encode_set {
                Ok(PyList::new(py, frame.result)?.into_any().unbind())
            } else if frame.is_set {
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

    pub(in crate::session) fn resume_array(
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
}
