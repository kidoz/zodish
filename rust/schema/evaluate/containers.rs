//! Synchronous container snapshots, construction, issue aggregation, and fast paths.

use pyo3::exceptions::PyRecursionError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

use super::is_primitive_kind;
use crate::MAX_DEPTH;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::{Kind, NativeSchema, UnknownKeys};
use crate::values::{deepcopy, primitive_matches};

impl NativeSchema {
    pub(super) fn evaluate_sequence(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let is_tuple = matches!(self.kind, Kind::Tuple);
        let is_set = matches!(self.kind, Kind::Set | Kind::SetEncode);
        let encode_set = matches!(self.kind, Kind::SetEncode);
        if is_set {
            if !value.is_instance_of::<PySet>() {
                let params = PyDict::new(py);
                params.set_item("expected", "set")?;
                return ctx.fail_with("invalid_type", "Expected set", path, params);
            }
        } else if !value.is_instance_of::<PyList>()
            && !(is_tuple && value.is_instance_of::<PyTuple>())
        {
            let expected = if is_tuple { "tuple or list" } else { "list" };
            let params = PyDict::new(py);
            params.set_item("expected", expected)?;
            return ctx.fail_with(
                "invalid_type",
                &format!("Expected {expected}"),
                path,
                params,
            );
        }
        let exact_list = if !is_tuple && !is_set && value.is_exact_instance_of::<PyList>() {
            Some(value.cast::<PyList>().map_err(PyErr::from)?)
        } else {
            None
        };
        if let Some(list) = &exact_list {
            let child_kind = self.children[0].try_borrow(py).map_err(PyErr::from)?.kind;
            if is_primitive_kind(child_kind) {
                if !list.is_empty() && depth + 1 >= MAX_DEPTH {
                    return Err(
                        PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into(),
                    );
                }
                if let Some(parsed) = fast_array_list(py, list, child_kind)? {
                    return Ok(parsed);
                }
            }
        }
        // Snapshot containers: a callback can mutate the original without
        // invalidating Rust's traversal or Python dictionary iterators.
        let mut items = ItemBuffer::new();
        if let Some(list) = &exact_list {
            for item in list.iter() {
                items.push(item);
            }
        } else if is_tuple && value.is_exact_instance_of::<PyTuple>() {
            for item in value.cast::<PyTuple>().map_err(PyErr::from)?.iter() {
                items.push(item);
            }
        } else {
            for item in value.try_iter()? {
                items.push(item?);
            }
        }
        if !is_tuple && !items.is_empty() && depth + 1 >= MAX_DEPTH {
            return Err(PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into());
        }
        let fixed = self.children.len();
        let rest_node = if is_tuple {
            let rest = self.args[0].bind(py);
            if rest.is_none() {
                if items.len() != fixed {
                    let params = PyDict::new(py);
                    params.set_item("expected", fixed)?;
                    return ctx.fail_with(
                        "invalid_length",
                        &format!("Expected {fixed} items"),
                        path,
                        params,
                    );
                }
                None
            } else {
                if items.len() < fixed {
                    let params = PyDict::new(py);
                    params.set_item("minimum", fixed)?;
                    params.set_item("inclusive", true)?;
                    params.set_item("origin", "array")?;
                    return ctx.fail_with(
                        "too_small",
                        &format!("Expected at least {fixed} items"),
                        path,
                        params,
                    );
                }
                Some(rest.extract::<PyRef<'_, Self>>().map_err(PyErr::from)?)
            }
        } else {
            None
        };
        let mut result = Vec::with_capacity(items.len());
        let mut issues = Vec::new();
        for index in 0..items.len() {
            let raw = items.get(index).expect("item");
            path.push(PathPart::Index(index));
            let parsed = if is_tuple && index >= fixed {
                rest_node
                    .as_ref()
                    .expect("rest schema")
                    .validate(ctx, raw, path, depth + 1)
            } else {
                self.child(if is_tuple { index } else { 0 }, ctx, raw, path, depth)
            };
            path.pop();
            match parsed {
                Ok(value) => result.push(value),
                Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                Err(error) => return Err(error),
            }
        }
        if !issues.is_empty() {
            return Err(Error::Invalid(issues));
        }
        if encode_set {
            Ok(PyList::new(py, result)?.into_any().unbind())
        } else if is_set {
            let set = PySet::empty(py)?;
            for value in result {
                set.add(value.bind(py))?;
            }
            Ok(set.into_any().unbind())
        } else if is_tuple {
            Ok(PyTuple::new(py, result)?.into_any().unbind())
        } else {
            Ok(PyList::new(py, result)?.into_any().unbind())
        }
    }

    pub(super) fn evaluate_mapping(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let Ok(dict) = value.cast::<PyDict>() else {
            let expected = if matches!(self.kind, Kind::Object) {
                "dict with string keys"
            } else {
                "dict"
            };
            let params = PyDict::new(py);
            params.set_item("expected", expected)?;
            return ctx.fail_with(
                "invalid_type",
                &format!("Expected {expected}"),
                path,
                params,
            );
        };
        let result = PyDict::new(py);
        let mut issues = Vec::new();
        if matches!(self.kind, Kind::Object) {
            let keys = self.args[0]
                .bind(py)
                .cast::<PyTuple>()
                .map_err(PyErr::from)?;
            let input_keys = self.args[3]
                .bind(py)
                .cast::<PyTuple>()
                .map_err(PyErr::from)?;
            const INLINE_VALUES: usize = 4;
            let policy = self.unknown_keys;
            let output_names = if matches!(policy, UnknownKeys::Passthrough | UnknownKeys::Catchall,)
            {
                Some(PySet::new(py, keys.iter())?)
            } else {
                None
            };
            let mut inline_values: [Option<Bound<'_, PyAny>>; INLINE_VALUES] =
                std::array::from_fn(|_| None);
            let mut heap_values = Vec::with_capacity(keys.len().saturating_sub(INLINE_VALUES));
            let mut present = 0;
            for (index, input_key) in input_keys.iter().enumerate() {
                let value = dict.get_item(&input_key)?;
                if value.is_some() {
                    present += 1;
                }
                if index < INLINE_VALUES {
                    inline_values[index] = value;
                } else {
                    heap_values.push(value);
                }
            }
            let snapshot = if present == dict.len() {
                None
            } else {
                Some(dict.copy()?)
            };
            for (index, key) in keys.iter().enumerate() {
                let input_key = input_keys.get_item(index)?;
                let value = if index < INLINE_VALUES {
                    inline_values[index].take()
                } else {
                    heap_values[index - INLINE_VALUES].take()
                };
                let is_present = value.is_some();
                let raw = value.unwrap_or_else(|| ctx.missing.clone());
                let child = self.children[index].try_borrow(py).map_err(PyErr::from)?;
                if is_present
                    && depth + 1 < MAX_DEPTH
                    && is_primitive_kind(child.kind)
                    && primitive_matches(&raw, child.kind)?
                {
                    result.set_item(&key, &raw)?;
                    continue;
                }
                let path_key = if matches!(child.kind, Kind::RequiredOutput) {
                    key.clone().unbind()
                } else {
                    input_key.clone().unbind()
                };
                path.push(PathPart::Value(path_key));
                let parsed = child.validate(ctx, &raw, path, depth + 1);
                path.pop();
                match parsed {
                    Ok(value) if !value.bind(py).is(&ctx.missing) => {
                        result.set_item(&key, value)?
                    }
                    Ok(_) => {}
                    Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                    Err(error) => return Err(error),
                }
            }
            if let Some(snapshot) = snapshot {
                let known = PyDict::new(py);
                for input_key in input_keys.iter() {
                    known.set_item(&input_key, true)?;
                }
                for (key, raw) in snapshot.iter() {
                    if !key.is_instance_of::<PyString>() {
                        let params = PyDict::new(py);
                        params.set_item("expected", "string")?;
                        issues.push(Issue::new(
                            py,
                            "invalid_key",
                            "Object keys must be strings",
                            path,
                            params.into_any(),
                        ));
                    } else if !known.contains(&key)? {
                        if policy == UnknownKeys::Strict {
                            let params = PyDict::new(py);
                            params
                                .set_item("keys", PyTuple::new(py, vec![key.clone().unbind()])?)?;
                            path.push(PathPart::Value(key.clone().unbind()));
                            issues.push(Issue::new(
                                py,
                                "unrecognized_key",
                                "Unrecognized key",
                                path,
                                params.into_any(),
                            ));
                            path.pop();
                        } else if policy == UnknownKeys::Passthrough {
                            let collides = match &output_names {
                                Some(names) => names.contains(&key)?,
                                None => false,
                            };
                            if !collides {
                                result.set_item(key, deepcopy(&raw)?)?;
                            }
                        } else if policy == UnknownKeys::Catchall {
                            let collides = match &output_names {
                                Some(names) => names.contains(&key)?,
                                None => false,
                            };
                            if collides {
                                continue;
                            }
                            let catchall = self.args[2].bind(py);
                            let node: PyRef<'_, Self> = catchall.extract().map_err(PyErr::from)?;
                            path.push(PathPart::Value(key.clone().unbind()));
                            let parsed = node.validate(ctx, &raw, path, depth + 1);
                            path.pop();
                            match parsed {
                                Ok(value) => result.set_item(key, value)?,
                                Err(Error::Invalid(mut errors)) => {
                                    issues.append(&mut errors);
                                }
                                Err(error) => return Err(error),
                            }
                        }
                    }
                }
            }
        } else {
            let snapshot = dict.copy()?;
            let key_schema = self.args[0].bind(py);
            let key_node = if key_schema.is_none() {
                None
            } else {
                Some(
                    key_schema
                        .extract::<PyRef<'_, Self>>()
                        .map_err(PyErr::from)?,
                )
            };
            for (key, raw) in snapshot.iter() {
                let parsed_key = if let Some(node) = key_node.as_ref() {
                    match node.validate(ctx, &key, path, depth + 1) {
                        Ok(key) => key,
                        Err(Error::Invalid(mut errors)) => {
                            issues.append(&mut errors);
                            continue;
                        }
                        Err(error) => return Err(error),
                    }
                } else {
                    if !key.is_instance_of::<PyString>() {
                        let params = PyDict::new(py);
                        params.set_item("expected", "string")?;
                        issues.push(Issue::new(
                            py,
                            "invalid_key",
                            "Record keys must be strings",
                            path,
                            params.into_any(),
                        ));
                        continue;
                    }
                    key.clone().unbind()
                };
                path.push(PathPart::Value(parsed_key.clone_ref(py)));
                let parsed = self.child(0, ctx, &raw, path, depth);
                path.pop();
                match parsed {
                    Ok(value) => result.set_item(parsed_key.bind(py), value)?,
                    Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                    Err(error) => return Err(error),
                }
            }
        }
        if !issues.is_empty() {
            return Err(Error::Invalid(issues));
        }
        Ok(result.into_any().unbind())
    }
}

const INLINE_ITEMS: usize = 4;

enum ItemBuffer<'py> {
    Inline([Option<Bound<'py, PyAny>>; INLINE_ITEMS], usize),
    Heap(Vec<Bound<'py, PyAny>>),
}

impl<'py> ItemBuffer<'py> {
    fn new() -> Self {
        Self::Inline(std::array::from_fn(|_| None), 0)
    }

    fn push(&mut self, item: Bound<'py, PyAny>) {
        if let Self::Inline(slots, len) = self {
            if *len < INLINE_ITEMS {
                slots[*len] = Some(item);
                *len += 1;
                return;
            }
            let mut values = Vec::with_capacity(INLINE_ITEMS + 1);
            for slot in slots.iter_mut() {
                if let Some(value) = slot.take() {
                    values.push(value);
                }
            }
            values.push(item);
            *self = Self::Heap(values);
            return;
        }
        if let Self::Heap(values) = self {
            values.push(item);
        }
    }

    fn len(&self) -> usize {
        match self {
            Self::Inline(_, len) => *len,
            Self::Heap(values) => values.len(),
        }
    }

    fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn get(&self, index: usize) -> Option<&Bound<'py, PyAny>> {
        match self {
            Self::Inline(slots, len) if index < *len => slots[index].as_ref(),
            Self::Heap(values) => values.get(index),
            _ => None,
        }
    }
}

fn fast_array_list(
    py: Python<'_>,
    list: &Bound<'_, PyList>,
    kind: Kind,
) -> PyResult<Option<Py<PyAny>>> {
    let mut parsed = Vec::with_capacity(list.len());
    for item in list.iter() {
        if !primitive_matches(&item, kind)? {
            return Ok(None);
        }
        parsed.push(item.unbind());
    }
    Ok(Some(PyList::new(py, parsed)?.into_any().unbind()))
}
