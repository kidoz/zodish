//! Synchronous native dataclass decode/encode evaluation and instance assembly.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyString, PyTuple, PyType};

use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::schema::NativeSchema;

pub(crate) const FLAG_HAS_DEFAULT: u8 = 1;
pub(crate) const FLAG_IS_INITVAR: u8 = 2;
pub(crate) const FLAG_HAS_DEFAULT_NODE: u8 = 4;

/// Immortal `object` allocation and assignment slot wrappers, resolved once;
/// the per-call import and attribute lookups dominate small classes otherwise.
struct ObjectHooks {
    new: Py<PyAny>,
    setattr: Py<PyAny>,
}

fn object_hooks(py: Python<'_>) -> PyResult<&'static ObjectHooks> {
    static HOOKS: PyOnceLock<ObjectHooks> = PyOnceLock::new();
    HOOKS.get_or_try_init(py, || {
        let object_cls = py.import("builtins")?.getattr("object")?;
        Ok(ObjectHooks {
            new: object_cls.getattr("__new__")?.unbind(),
            setattr: object_cls.getattr("__setattr__")?.unbind(),
        })
    })
}

/// Allocate an instance without calling its initializer, populate the stored
/// fields through `object.__setattr__` (frozen and slots classes included),
/// then run `__post_init__` with the InitVar values in declaration order;
/// `has_post_init` is the dataclass node's decoration-time flag. Shared by the
/// synchronous evaluator and the async session frame so the two cannot drift.
pub(crate) fn assemble_instance(
    py: Python<'_>,
    cls: &Bound<'_, PyType>,
    stored: &Bound<'_, PyDict>,
    initvar_values: &[Py<PyAny>],
    has_post_init: bool,
) -> PyResult<Py<PyAny>> {
    let hooks = object_hooks(py)?;
    let instance = hooks.new.bind(py).call1((cls,))?;
    let setattr = hooks.setattr.bind(py);
    for (name, value) in stored.iter() {
        setattr.call1((&instance, name, value))?;
    }
    if has_post_init {
        let post_init = instance.getattr("__post_init__")?;
        if !post_init.is_none() {
            let args = PyTuple::new(py, initvar_values.iter().map(|value| value.clone_ref(py)))?;
            post_init.call1(args)?;
        }
    }
    Ok(instance.unbind())
}

pub(crate) fn missing_default_error<T>(name: &Bound<'_, PyAny>, message: &str) -> Result<T> {
    let repr = name.repr()?;
    Err(PyTypeError::new_err(format!("Field {} {message}", repr.to_str()?)).into())
}

/// Resolve one omitted field's default: the static value by identity against
/// the MISSING sentinel, otherwise one factory call; a factory producing
/// MISSING is a TypeError naming the field. Shared by the synchronous
/// evaluator and the async session frame so the two cannot drift.
pub(crate) fn resolve_default(
    ctx: &Context<'_>,
    values: &Bound<'_, PyTuple>,
    factories: &Bound<'_, PyTuple>,
    index: usize,
    name: &Bound<'_, PyAny>,
) -> Result<Py<PyAny>> {
    let default = values.get_item(index)?;
    if !default.is(&ctx.missing) {
        return Ok(default.unbind());
    }
    let produced = factories.get_item(index)?.call0()?;
    if produced.is(&ctx.missing) {
        return missing_default_error(name, "factory produced MISSING");
    }
    Ok(produced.unbind())
}

impl NativeSchema {
    pub(super) fn evaluate_dataclass_decode(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let Ok(dict) = value.cast::<PyDict>() else {
            let params = PyDict::new(py);
            params.set_item("expected", "dict with string keys")?;
            return ctx.fail_with(
                "invalid_type",
                "Expected dict with string keys",
                path,
                params,
            );
        };
        let names = self.args[1]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let flags = self.args[2]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let default_values = self.args[3]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let default_factories = self.args[4]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let non_init_names = self.args[5]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let has_post_init: bool = self.args[6].bind(py).extract()?;
        let input_names = self.args[9]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let field_count = names.len();
        // Read every field value before evaluating: a field callback can
        // mutate the input dict, so reads and the snapshot follow the object
        // node's fetch-then-copy order. Lookups use the wire (input) key, like
        // the object node's input_keys.
        let mut raws = Vec::with_capacity(field_count);
        let mut present = 0;
        for index in 0..field_count {
            let raw = dict.get_item(input_names.get_item(index)?)?;
            if raw.is_some() {
                present += 1;
            }
            raws.push(raw);
        }
        let snapshot = if present == dict.len() {
            None
        } else {
            Some(dict.copy()?)
        };
        let stored = PyDict::new(py);
        let mut initvar_values: Vec<Py<PyAny>> = Vec::new();
        let mut issues = Vec::new();
        for (index, raw) in raws.iter().enumerate() {
            let name = names.get_item(index)?;
            let input_name = input_names.get_item(index)?;
            let flag = flags.get_item(index)?.extract::<u8>()?;
            let mut supplied = None;
            if let Some(raw) = raw {
                path.push(PathPart::Value(input_name.clone().unbind()));
                let parsed = self.child(2 * index, ctx, raw, path, depth);
                path.pop();
                match parsed {
                    Ok(value) if !value.bind(py).is(&ctx.missing) => supplied = Some(value),
                    Ok(_) => {}
                    Err(Error::Invalid(mut errors)) => {
                        issues.append(&mut errors);
                        continue;
                    }
                    Err(error) => return Err(error),
                }
            }
            let value = match supplied {
                Some(value) => value,
                None => {
                    if flag & FLAG_HAS_DEFAULT == 0 {
                        path.push(PathPart::Value(input_name.clone().unbind()));
                        issues.push(Issue::simple(py, "required", "Required", path));
                        path.pop();
                        continue;
                    }
                    let resolved =
                        resolve_default(ctx, default_values, default_factories, index, &name)?;
                    if flag & FLAG_HAS_DEFAULT_NODE == 0 {
                        resolved
                    } else {
                        path.push(PathPart::Value(name.clone().unbind()));
                        let parsed = self.child(2 * index + 1, ctx, resolved.bind(py), path, depth);
                        path.pop();
                        match parsed {
                            Ok(value) => {
                                if value.bind(py).is(&ctx.missing) {
                                    return missing_default_error(
                                        &name,
                                        "default produced MISSING",
                                    );
                                }
                                value
                            }
                            Err(Error::Invalid(mut errors)) => {
                                issues.append(&mut errors);
                                continue;
                            }
                            Err(error) => return Err(error),
                        }
                    }
                }
            };
            if flag & FLAG_IS_INITVAR != 0 {
                initvar_values.push(value);
            } else {
                stored.set_item(&name, value)?;
            }
        }
        let non_init_default_values = self.args[7]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let non_init_default_factories = self.args[8]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        for index in 0..non_init_names.len() {
            let name = non_init_names.get_item(index)?;
            let resolved = resolve_default(
                ctx,
                non_init_default_values,
                non_init_default_factories,
                index,
                &name,
            )?;
            path.push(PathPart::Value(name.clone().unbind()));
            let parsed = self.child(2 * field_count + index, ctx, resolved.bind(py), path, depth);
            path.pop();
            match parsed {
                Ok(value) => {
                    if value.bind(py).is(&ctx.missing) {
                        return missing_default_error(&name, "default produced MISSING");
                    }
                    stored.set_item(&name, value)?;
                }
                Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                Err(error) => return Err(error),
            }
        }
        if let Some(snapshot) = snapshot {
            for (key, _) in snapshot.iter() {
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
                }
            }
        }
        if !issues.is_empty() {
            return Err(Error::Invalid(issues));
        }
        let cls = self.args[0]
            .bind(py)
            .cast::<PyType>()
            .map_err(PyErr::from)?;
        Ok(assemble_instance(
            py,
            cls,
            &stored,
            &initvar_values,
            has_post_init,
        )?)
    }

    pub(super) fn evaluate_dataclass_encode(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let cls = self.args[0].bind(py);
        if !value.is_instance(cls)? {
            let name: String = cls.getattr("__name__")?.extract()?;
            let params = PyDict::new(py);
            params.set_item("expected", &name)?;
            return ctx.fail_with("invalid_type", &format!("Expected {name}"), path, params);
        }
        let names = self.args[1]
            .bind(py)
            .cast::<PyTuple>()
            .map_err(PyErr::from)?;
        let result = PyDict::new(py);
        let mut issues = Vec::new();
        for (index, name) in names.iter().enumerate() {
            let raw = value.getattr(name.cast::<PyString>().map_err(PyErr::from)?)?;
            path.push(PathPart::Value(name.clone().unbind()));
            let parsed = self.child(index, ctx, &raw, path, depth);
            path.pop();
            match parsed {
                Ok(value) => result.set_item(&name, value)?,
                Err(Error::Invalid(mut errors)) => issues.append(&mut errors),
                Err(error) => return Err(error),
            }
        }
        if !issues.is_empty() {
            return Err(Error::Invalid(issues));
        }
        Ok(result.into_any().unbind())
    }
}
