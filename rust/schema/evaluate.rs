//! Synchronous schema dispatch, primitive validation, composition, and depth checks.

mod containers;

use pyo3::exceptions::{PyRecursionError, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyTuple};

use super::{
    Kind, NativeSchema, evaluate_string_bool, interpret_super_refine, select_discriminated_branch,
};
use crate::MAX_DEPTH;
use crate::errors::{Error, Issue, Path, PathPart, Result};
use crate::runtime::{Context, LazyStore};
use crate::stack::ensure_stack;
use crate::values::{
    coerce_value, deepcopy, enum_message, is_json_value, literal_description, merge_dicts,
    safe_param,
};

impl NativeSchema {
    pub(crate) fn validate(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        if depth >= MAX_DEPTH {
            return Err(PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into());
        }
        ensure_stack()?;
        self.evaluate(ctx, value, path, depth)
            .map_err(|error| ctx.normalize(error, path))
    }

    fn child(
        &self,
        index: usize,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let child = self.children[index]
            .try_borrow(ctx.py)
            .map_err(PyErr::from)?;
        child.validate(ctx, value, path, depth + 1)
    }

    fn evaluate(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        let missing = value.is(&ctx.missing);
        let keep = || Ok(value.clone().unbind());
        match self.kind {
            Kind::Cleared => Err(PyRuntimeError::new_err("Native schema has been cleared").into()),
            Kind::AsyncRefine
            | Kind::AsyncSuperRefine
            | Kind::AsyncTransform
            | Kind::AsyncPreprocess
            | Kind::AsyncDefaultFactory
            | Kind::AsyncCatch
            | Kind::AsyncOverwrite => Err(PyTypeError::new_err(
                "Async validation must be performed with parse_async()",
            )
            .into()),
            Kind::Optional if missing => keep(),
            Kind::Nullable if value.is_none() => keep(),
            Kind::Optional | Kind::Nullable | Kind::Wire => self.child(0, ctx, value, path, depth),
            Kind::NonOptional => {
                if missing {
                    ctx.fail("required", "Required", path)
                } else {
                    self.child(0, ctx, value, path, depth)
                }
            }
            Kind::Default | Kind::DefaultFactory => {
                if missing {
                    let default = if matches!(self.kind, Kind::Default) {
                        deepcopy(self.args[0].bind(py))?
                    } else {
                        self.args[0].bind(py).call0()?
                    };
                    self.child(0, ctx, &default, path, depth)
                } else {
                    self.child(0, ctx, value, path, depth)
                }
            }
            Kind::Lazy => {
                let key = self as *const Self as usize;
                let node = match ctx.lazy_store.last(py, key) {
                    Some(node) => node,
                    None => match ctx.lazy_store.find(py, key) {
                        Some(node) => {
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            node
                        }
                        None => {
                            let schema = self.args[0].bind(py).call0()?.getattr("_node")?;
                            let node: Py<NativeSchema> = schema
                                .cast_into::<NativeSchema>()
                                .map_err(PyErr::from)?
                                .unbind();
                            ctx.lazy_store.push(key, node.clone_ref(py));
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            node
                        }
                    },
                };
                node.try_borrow(py)
                    .map_err(PyErr::from)?
                    .validate(ctx, value, path, depth + 1)
            }
            Kind::Catch => match self.child(0, ctx, value, path, depth) {
                Ok(value) => Ok(value),
                Err(Error::Invalid(_)) => {
                    let fallback = self.args[0].bind(py);
                    if fallback.is_callable() {
                        Ok(fallback.call0()?.unbind())
                    } else {
                        Ok(deepcopy(fallback)?.unbind())
                    }
                }
                Err(Error::Python(error)) => Err(Error::Python(error)),
            },
            Kind::Coerce => {
                if missing {
                    return self.child(0, ctx, value, path, depth);
                }
                let target: String = self.args[0].bind(py).extract()?;
                let Some(converted) = coerce_value(py, &target, value)? else {
                    let expected = format!("coercible {target}");
                    let params = PyDict::new(py);
                    params.set_item("expected", &expected)?;
                    return ctx.fail_with(
                        "invalid_type",
                        &format!("Expected {expected}"),
                        path,
                        params,
                    );
                };
                self.child(0, ctx, &converted, path, depth)
            }
            Kind::Preprocess => {
                let processed = self.args[0].bind(py).call1((value,))?;
                self.child(0, ctx, &processed, path, depth)
            }
            Kind::Refine => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                if self.args[0].bind(py).call1((parsed,))?.is_truthy()? {
                    return Ok(parsed.clone().unbind());
                }
                let message: String = self.args[1].bind(py).extract()?;
                let relative = self.args[2].bind(py);
                let mut issue_path = path
                    .iter()
                    .map(|part| part.clone_ref(py))
                    .collect::<Vec<_>>();
                for part in relative.try_iter()? {
                    issue_path.push(PathPart::Value(part?.unbind()));
                }
                ctx.fail("custom", &message, &issue_path)
            }
            Kind::SuperRefine => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let result = self.args[0].bind(py).call1((parsed.bind(py),))?;
                interpret_super_refine(ctx, parsed, &result, path, "super_refine")
            }
            Kind::Transform
            | Kind::RequiredOutput
            | Kind::Overwrite
            | Kind::LengthMin
            | Kind::LengthMax
            | Kind::Min
            | Kind::Max
            | Kind::ExclusiveMin
            | Kind::ExclusiveMax
            | Kind::MultipleOf
            | Kind::Positive
            | Kind::Integral
            | Kind::Regex
            | Kind::StartsWith
            | Kind::EndsWith
            | Kind::Includes
            | Kind::Ipv6
            | Kind::Cidrv4
            | Kind::Cidrv6
            | Kind::Trim
            | Kind::Lower
            | Kind::Upper => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                self.evaluate_post_child(ctx, parsed, path)
            }
            Kind::Pipe => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                self.child(1, ctx, parsed.bind(py), path, depth)
            }
            _ if missing && !matches!(self.kind, Kind::Union) => {
                ctx.fail("required", "Required", path)
            }
            Kind::StringBool => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                evaluate_string_bool(ctx, self, parsed.bind(py), path)
            }
            Kind::ExactInstance | Kind::NativeEnum => {
                let cls = self.args[0].bind(py);
                if value.get_type().is(cls) {
                    keep()
                } else {
                    let name: String = cls.getattr("__name__")?.extract()?;
                    let params = PyDict::new(py);
                    params.set_item("expected", &name)?;
                    ctx.fail_with("invalid_type", &format!("Expected {name}"), path, params)
                }
            }
            Kind::Json => {
                if is_json_value(value, 0)? {
                    keep()
                } else {
                    let params = PyDict::new(py);
                    params.set_item("expected", "JSON value")?;
                    ctx.fail_with("invalid_type", "Expected JSON value", path, params)
                }
            }
            Kind::Enum => {
                let choices = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                for choice in choices.iter() {
                    if value.get_type().is(choice.get_type()) && value.eq(&choice)? {
                        return keep();
                    }
                }
                if let Some(template) = &self.error_template {
                    return ctx.fail_template(template, path);
                }
                let params = PyDict::new(py);
                let values = PyTuple::new(
                    py,
                    choices
                        .iter()
                        .map(|choice| safe_param(py, &choice).map(Bound::unbind))
                        .collect::<PyResult<Vec<_>>>()?,
                )?;
                params.set_item("values", values)?;
                let message = enum_message(choices)?;
                ctx.fail_with("invalid_value", &message, path, params)
            }
            Kind::Literal => {
                let expected = self.args[0].bind(py);
                if value.get_type().is(expected.get_type()) && value.eq(expected)? {
                    return keep();
                }
                if let Some(template) = &self.error_template {
                    return ctx.fail_template(template, path);
                }
                let message = format!("Expected {}", literal_description(expected)?);
                let params = PyDict::new(py);
                let values = PyTuple::new(py, vec![safe_param(py, expected)?.unbind()])?;
                params.set_item("values", values)?;
                ctx.fail_with("invalid_value", &message, path, params)
            }
            Kind::String
            | Kind::Number
            | Kind::Integer
            | Kind::Boolean
            | Kind::Unknown
            | Kind::None
            | Kind::Instance => {
                let (valid, name) = match self.kind {
                    Kind::String => (value.is_instance_of::<PyString>(), "string".into()),
                    Kind::Integer => (value.is_exact_instance_of::<PyInt>(), "integer".into()),
                    Kind::Number => (
                        value.is_exact_instance_of::<PyInt>()
                            || (value.is_exact_instance_of::<PyFloat>()
                                && value.extract::<f64>()?.is_finite()),
                        "finite number".into(),
                    ),
                    Kind::Boolean => (value.is_exact_instance_of::<PyBool>(), "boolean".into()),
                    Kind::Unknown => (true, "value".into()),
                    Kind::None => (value.is_none(), "None".into()),
                    Kind::Instance => {
                        let cls = self.args[0].bind(py);
                        (
                            value.is_instance(cls)?,
                            cls.getattr("__name__")?.extract::<String>()?,
                        )
                    }
                    _ => unreachable!(),
                };
                if valid {
                    keep()
                } else if let Some(template) = &self.error_template {
                    ctx.fail_template(template, path)
                } else {
                    let params = PyDict::new(py);
                    params.set_item("expected", &name)?;
                    ctx.fail_with("invalid_type", &format!("Expected {name}"), path, params)
                }
            }
            Kind::Array | Kind::Set | Kind::SetEncode | Kind::Tuple => {
                self.evaluate_sequence(ctx, value, path, depth)
            }
            Kind::Object | Kind::Record => self.evaluate_mapping(ctx, value, path, depth),
            Kind::DiscriminatedUnion => {
                let key = self.args[0].bind(py);
                let options = self.args[1]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let index = select_discriminated_branch(ctx, value, key, options, path)?;
                self.child(index, ctx, value, path, depth)
            }
            Kind::Intersection => {
                let left = self.child(0, ctx, value, path, depth);
                let right = self.child(1, ctx, value, path, depth);
                match (left, right) {
                    (Ok(left), Ok(right)) => {
                        let left = left.bind(py);
                        let right = right.bind(py);
                        if !left.is_instance_of::<PyDict>() || !right.is_instance_of::<PyDict>() {
                            return ctx.fail(
                                "invalid_intersection_types",
                                "Intersection outputs must be dictionaries",
                                path,
                            );
                        }
                        let merged = merge_dicts(
                            left.cast::<PyDict>().map_err(PyErr::from)?,
                            right.cast::<PyDict>().map_err(PyErr::from)?,
                            0,
                        )?;
                        Ok(merged)
                    }
                    (
                        Err(Error::Invalid(mut left_issues)),
                        Err(Error::Invalid(mut right_issues)),
                    ) => {
                        left_issues.append(&mut right_issues);
                        Err(Error::Invalid(left_issues))
                    }
                    (Err(Error::Invalid(issues)), Ok(_)) | (Ok(_), Err(Error::Invalid(issues))) => {
                        Err(Error::Invalid(issues))
                    }
                    (Err(Error::Python(error)), _) | (_, Err(Error::Python(error))) => {
                        Err(Error::Python(error))
                    }
                }
            }
            Kind::Union => {
                let mut branches = Vec::new();
                for index in 0..self.children.len() {
                    match self.child(index, ctx, value, path, depth) {
                        Ok(value) => return Ok(value),
                        Err(Error::Invalid(issues)) => branches.push(issues),
                        Err(error) => return Err(error),
                    }
                }
                let mut issue = Issue::simple(py, "invalid_union", "No union branch matched", path);
                issue.branches = branches;
                Err(Error::Invalid(vec![issue]))
            }
        }
    }
}

pub(crate) fn is_primitive_kind(kind: Kind) -> bool {
    kind.spec().primitive
}
