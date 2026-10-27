//! Post-child validation checks and reusable error templates shared by evaluators.

use std::net::{Ipv4Addr, Ipv6Addr};
use std::sync::Arc;

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyInt, PyList, PySet, PyString, PyTuple};

use super::{Kind, NativeSchema};
use crate::errors::{Error, ErrorTemplate, Issue, Path, PathPart, Result};
use crate::runtime::Context;
use crate::values::{enum_message, is_multiple_of, literal_description, safe_param};

/// Interpret a `super_refine` or `super_refine_async` callback result.
///
/// The synchronous evaluator, session wrapper continuation, and awaited async
/// callback path share this so Issue handling and path prefixing cannot drift.
pub(crate) fn interpret_super_refine(
    ctx: &Context<'_>,
    parsed: Py<PyAny>,
    result: &Bound<'_, PyAny>,
    path: &Path,
    callback: &str,
) -> Result<Py<PyAny>> {
    let py = ctx.py;
    if result.is_none() {
        return Ok(parsed);
    }
    let mut issues = Vec::new();
    if result.is_instance(&ctx.issue_type)? {
        issues.push(Issue::from_python(result)?);
    } else if result.is_instance_of::<PyList>() || result.is_instance_of::<PyTuple>() {
        for item in result.try_iter()? {
            let item = item?;
            if !item.is_instance(&ctx.issue_type)? {
                return Err(PyTypeError::new_err(format!(
                    "{callback} callback returned a non-Issue value"
                ))
                .into());
            }
            issues.push(Issue::from_python(&item)?);
        }
    } else {
        return Err(PyTypeError::new_err(format!(
            "{callback} callback must return Issue, a list or tuple of Issue, or None"
        ))
        .into());
    }
    if issues.is_empty() {
        return Ok(parsed);
    }
    for issue in &mut issues {
        let mut absolute = path
            .iter()
            .map(|part| part.clone_ref(py))
            .collect::<Vec<_>>();
        absolute.extend(issue.path.iter().map(|part| part.clone_ref(py)));
        issue.path = absolute;
    }
    Err(Error::Invalid(issues))
}

/// Convert an already validated string through a `string_bool` node.
///
/// The synchronous evaluator and the async continuation share this so the
/// accepted values and error payload cannot drift.
pub(crate) fn evaluate_string_bool(
    ctx: &Context<'_>,
    node: &NativeSchema,
    parsed: &Bound<'_, PyAny>,
    path: &Path,
) -> Result<Py<PyAny>> {
    let py = ctx.py;
    let text: String = parsed.extract()?;
    let case_sensitive: bool = node.args[2].bind(py).extract()?;
    let normalized = if case_sensitive {
        text
    } else {
        text.to_lowercase()
    };
    for item in node.args[0].bind(py).try_iter()? {
        let candidate: String = item?.extract()?;
        let candidate = if case_sensitive {
            candidate
        } else {
            candidate.to_lowercase()
        };
        if normalized == candidate {
            return Ok(true.into_pyobject(py)?.to_owned().into_any().unbind());
        }
    }
    for item in node.args[1].bind(py).try_iter()? {
        let candidate: String = item?.extract()?;
        let candidate = if case_sensitive {
            candidate
        } else {
            candidate.to_lowercase()
        };
        if normalized == candidate {
            return Ok(false.into_pyobject(py)?.to_owned().into_any().unbind());
        }
    }
    let message: String = node.args[3].bind(py).extract()?;
    let truthy = node.args[0]
        .bind(py)
        .cast::<PyTuple>()
        .map_err(PyErr::from)?;
    let falsy = node.args[1]
        .bind(py)
        .cast::<PyTuple>()
        .map_err(PyErr::from)?;
    let values = truthy
        .iter()
        .chain(falsy.iter())
        .map(Bound::unbind)
        .collect::<Vec<_>>();
    let values = PyTuple::new(py, values)?;
    let params = PyDict::new(py);
    params.set_item("values", values)?;
    ctx.fail_with("invalid_value", &message, path, params)
}

/// Select the branch index for a discriminated union.
///
/// The synchronous evaluator and the async node evaluator share this so
/// discriminator lookup, exact type/value matching, and failure paths cannot
/// drift between parsing modes.
pub(crate) fn select_discriminated_branch(
    ctx: &Context<'_>,
    value: &Bound<'_, PyAny>,
    key: &Bound<'_, PyAny>,
    options: &Bound<'_, PyTuple>,
    path: &Path,
) -> Result<usize> {
    let py = ctx.py;
    if value.is(&ctx.missing) {
        return Err(context_failure(ctx.fail("required", "Required", path)));
    }
    let Ok(dict) = value.cast::<PyDict>() else {
        let params = PyDict::new(py);
        params.set_item("expected", "dict with string keys")?;
        return Err(context_failure(ctx.fail_with(
            "invalid_type",
            "Expected dict with string keys",
            path,
            params,
        )));
    };
    let Some(raw) = dict.get_item(key)? else {
        let mut issue_path = path
            .iter()
            .map(|part| part.clone_ref(py))
            .collect::<Vec<_>>();
        issue_path.push(PathPart::Value(key.clone().unbind()));
        return Err(context_failure(ctx.fail(
            "required",
            "Required",
            &issue_path,
        )));
    };
    for (index, option) in options.iter().enumerate() {
        if raw.get_type().is(option.get_type()) && raw.eq(&option)? {
            return Ok(index);
        }
    }
    let message = enum_message(options)?;
    let params = PyDict::new(py);
    params.set_item("values", options)?;
    let mut issue_path = path
        .iter()
        .map(|part| part.clone_ref(py))
        .collect::<Vec<_>>();
    issue_path.push(PathPart::Value(key.clone().unbind()));
    Err(context_failure(ctx.fail_with(
        "invalid_value",
        &message,
        &issue_path,
        params,
    )))
}

fn context_failure(result: Result<Py<PyAny>>) -> Error {
    match result {
        Err(error) => error,
        Ok(_) => unreachable!("context failures always return an error"),
    }
}

impl NativeSchema {
    /// Apply a unary post-child operation to an already validated value.
    /// The synchronous evaluator and async continuation frames share this so
    /// constraint behavior cannot drift between parsing modes.
    pub(crate) fn evaluate_post_child(
        &self,
        ctx: &Context<'_>,
        parsed: &Bound<'_, PyAny>,
        path: &Path,
    ) -> Result<Py<PyAny>> {
        let py = ctx.py;
        if matches!(self.kind, Kind::RequiredOutput) {
            return if parsed.is(&ctx.missing) {
                ctx.fail("required", "Required", path)
            } else {
                Ok(parsed.clone().unbind())
            };
        }
        let valid = match self.kind {
            Kind::Transform | Kind::Overwrite => {
                return Ok(self.args[0].bind(py).call1((parsed,))?.unbind());
            }
            Kind::ContextTransform => {
                return Ok(self.args[0]
                    .bind(py)
                    .call1((parsed, &ctx.callback_context))?
                    .unbind());
            }
            Kind::Trim | Kind::Lower | Kind::Upper => {
                let method = match self.kind {
                    Kind::Trim => "strip",
                    Kind::Lower => "lower",
                    _ => "upper",
                };
                return Ok(py
                    .get_type::<PyString>()
                    .call_method1(method, (parsed,))?
                    .unbind());
            }
            Kind::LengthMin | Kind::LengthMax => {
                let length = parsed.len()?.into_pyobject(py)?.into_any();
                if matches!(self.kind, Kind::LengthMin) {
                    length.ge(self.args[0].bind(py))?
                } else {
                    length.le(self.args[0].bind(py))?
                }
            }
            Kind::Min => parsed.ge(self.args[0].bind(py))?,
            Kind::Max => parsed.le(self.args[0].bind(py))?,
            Kind::ExclusiveMin => parsed.gt(self.args[0].bind(py))?,
            Kind::ExclusiveMax => parsed.lt(self.args[0].bind(py))?,
            Kind::MultipleOf => is_multiple_of(parsed, self.args[0].bind(py))?,
            Kind::Positive => parsed.gt(0)?,
            Kind::Integral => {
                parsed.is_exact_instance_of::<PyInt>() || parsed.extract::<f64>()?.fract() == 0.0
            }
            Kind::Regex => {
                let matched = !self.args[0]
                    .bind(py)
                    .call_method1("search", (parsed,))?
                    .is_none();
                let format = self.args[2].bind(py).extract::<String>()?;
                matched && valid_calendar_date(parsed, &format)?
            }
            Kind::StartsWith => py
                .get_type::<PyString>()
                .call_method1("startswith", (parsed, self.args[0].bind(py)))?
                .is_truthy()?,
            Kind::EndsWith => py
                .get_type::<PyString>()
                .call_method1("endswith", (parsed, self.args[0].bind(py)))?
                .is_truthy()?,
            Kind::Includes => {
                py.get_type::<PyString>()
                    .call_method1("find", (parsed, self.args[0].bind(py)))?
                    .extract::<isize>()?
                    >= 0
            }
            Kind::Ipv6 => parsed
                .extract::<String>()
                .ok()
                .is_some_and(|text| text.parse::<Ipv6Addr>().is_ok()),
            Kind::Cidrv4 => parsed
                .extract::<String>()
                .ok()
                .is_some_and(|text| is_cidr_v4(&text)),
            Kind::Cidrv6 => parsed
                .extract::<String>()
                .ok()
                .is_some_and(|text| is_cidr_v6(&text)),
            _ => unreachable!(),
        };
        if valid {
            return Ok(parsed.clone().unbind());
        }
        if let Some(template) = &self.error_template {
            return ctx.fail_template(template, path);
        }
        let (code, message, params) = match self.kind {
            Kind::LengthMin => {
                let params = PyDict::new(py);
                params.set_item("minimum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item("origin", length_origin(parsed))?;
                (
                    "too_small",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::LengthMax => {
                let params = PyDict::new(py);
                params.set_item("maximum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item("origin", length_origin(parsed))?;
                (
                    "too_big",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Min => {
                let params = PyDict::new(py);
                params.set_item("minimum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item("origin", "number")?;
                (
                    "too_small",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Max => {
                let params = PyDict::new(py);
                params.set_item("maximum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", true)?;
                params.set_item("origin", "number")?;
                (
                    "too_big",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::ExclusiveMin => {
                let params = PyDict::new(py);
                params.set_item("minimum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", false)?;
                params.set_item("origin", "number")?;
                (
                    "too_small",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::ExclusiveMax => {
                let params = PyDict::new(py);
                params.set_item("maximum", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("inclusive", false)?;
                params.set_item("origin", "number")?;
                (
                    "too_big",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::MultipleOf => {
                let params = PyDict::new(py);
                params.set_item("divisor", safe_param(py, self.args[0].bind(py))?)?;
                params.set_item("origin", "number")?;
                (
                    "not_multiple_of",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::StartsWith => {
                let params = PyDict::new(py);
                params.set_item("format", "starts_with")?;
                params.set_item("prefix", self.args[0].bind(py))?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::EndsWith => {
                let params = PyDict::new(py);
                params.set_item("format", "ends_with")?;
                params.set_item("suffix", self.args[0].bind(py))?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Includes => {
                let params = PyDict::new(py);
                params.set_item("format", "includes")?;
                params.set_item("substring", self.args[0].bind(py))?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Ipv6 => {
                let params = PyDict::new(py);
                params.set_item("format", "ipv6")?;
                (
                    "invalid_format",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Cidrv4 | Kind::Cidrv6 => {
                let params = PyDict::new(py);
                params.set_item(
                    "format",
                    if matches!(self.kind, Kind::Cidrv4) {
                        "cidrv4"
                    } else {
                        "cidrv6"
                    },
                )?;
                (
                    "invalid_format",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Positive => {
                let params = PyDict::new(py);
                params.set_item("minimum", 0)?;
                params.set_item("inclusive", false)?;
                params.set_item("origin", "number")?;
                (
                    "too_small",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Integral => {
                let params = PyDict::new(py);
                params.set_item("expected", "integer")?;
                (
                    "invalid_type",
                    self.args[0].bind(py).extract::<String>()?,
                    params,
                )
            }
            Kind::Regex => {
                let params = PyDict::new(py);
                params.set_item("format", self.args[2].bind(py).extract::<String>()?)?;
                (
                    "invalid_format",
                    self.args[1].bind(py).extract::<String>()?,
                    params,
                )
            }
            _ => unreachable!(),
        };
        ctx.fail_with(code, &message, path, params)
    }
}

fn valid_calendar_date(value: &Bound<'_, PyAny>, format: &str) -> PyResult<bool> {
    if !matches!(format, "date" | "date-time") {
        return Ok(true);
    }
    let text = value.extract::<String>()?;
    let bytes = text.as_bytes();
    let component = |start, end| {
        bytes.get(start..end).and_then(|part| {
            if part.iter().all(|byte| byte.is_ascii_digit()) {
                std::str::from_utf8(part).ok()?.parse::<u32>().ok()
            } else {
                None
            }
        })
    };
    let (Some(year), Some(month), Some(day)) = (component(0, 4), component(5, 7), component(8, 10))
    else {
        return Ok(false);
    };
    if year == 0 || month == 0 || day == 0 {
        return Ok(false);
    }
    let leap_year = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let maximum_day = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap_year => 29,
        2 => 28,
        _ => return Ok(false),
    };
    Ok(day <= maximum_day)
}

fn length_origin(parsed: &Bound<'_, PyAny>) -> &'static str {
    if parsed.is_instance_of::<PyString>() {
        "string"
    } else if parsed.is_instance_of::<PySet>() {
        "set"
    } else if parsed.is_instance_of::<PyBytes>() {
        "bytes"
    } else {
        "array"
    }
}

fn cidr_address(text: &str, maximum: u8) -> Option<&str> {
    let (address, prefix) = text.split_once('/')?;
    let max_digits = if maximum > 99 { 3 } else { 2 };
    if prefix.is_empty()
        || prefix.len() > max_digits
        || (prefix.len() > 1 && prefix.starts_with('0'))
        || !prefix.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let prefix = prefix.parse::<u8>().ok()?;
    (prefix <= maximum).then_some(address)
}

fn is_cidr_v4(text: &str) -> bool {
    cidr_address(text, 32).is_some_and(|address| address.parse::<Ipv4Addr>().is_ok())
}

fn is_cidr_v6(text: &str) -> bool {
    cidr_address(text, 128).is_some_and(|address| address.parse::<Ipv6Addr>().is_ok())
}

pub(super) fn build_error_template(
    py: Python<'_>,
    kind: Kind,
    children: &[Py<NativeSchema>],
    args: &[Py<PyAny>],
) -> PyResult<Option<ErrorTemplate>> {
    let template = |code: &str, message: String, params: Bound<'_, PyDict>| -> ErrorTemplate {
        ErrorTemplate {
            code: Arc::from(code),
            message: Arc::from(message),
            params: params.into_any().unbind(),
        }
    };
    let template = match kind {
        Kind::Positive => {
            let params = PyDict::new(py);
            params.set_item("minimum", 0)?;
            params.set_item("inclusive", false)?;
            params.set_item("origin", "number")?;
            template("too_small", args[0].bind(py).extract::<String>()?, params)
        }
        Kind::LengthMin | Kind::LengthMax => {
            let origin = match children[0].try_borrow(py).map_err(PyErr::from)?.kind {
                Kind::String => "string",
                Kind::Array => "array",
                _ => return Ok(None),
            };
            let params = PyDict::new(py);
            if matches!(kind, Kind::LengthMin) {
                params.set_item("minimum", safe_param(py, args[0].bind(py))?)?;
            } else {
                params.set_item("maximum", safe_param(py, args[0].bind(py))?)?;
            }
            params.set_item("inclusive", true)?;
            params.set_item("origin", origin)?;
            template(
                if matches!(kind, Kind::LengthMin) {
                    "too_small"
                } else {
                    "too_big"
                },
                args[1].bind(py).extract::<String>()?,
                params,
            )
        }
        Kind::Min | Kind::Max | Kind::ExclusiveMin | Kind::ExclusiveMax => {
            let params = PyDict::new(py);
            match kind {
                Kind::Min => params.set_item("minimum", safe_param(py, args[0].bind(py))?)?,
                Kind::Max => params.set_item("maximum", safe_param(py, args[0].bind(py))?)?,
                Kind::ExclusiveMin => {
                    params.set_item("minimum", safe_param(py, args[0].bind(py))?)?
                }
                Kind::ExclusiveMax => {
                    params.set_item("maximum", safe_param(py, args[0].bind(py))?)?
                }
                _ => unreachable!(),
            }
            params.set_item("inclusive", matches!(kind, Kind::Min | Kind::Max))?;
            params.set_item("origin", "number")?;
            template(
                if matches!(kind, Kind::Min | Kind::ExclusiveMin) {
                    "too_small"
                } else {
                    "too_big"
                },
                args[1].bind(py).extract::<String>()?,
                params,
            )
        }
        Kind::MultipleOf => {
            let params = PyDict::new(py);
            params.set_item("divisor", safe_param(py, args[0].bind(py))?)?;
            params.set_item("origin", "number")?;
            template(
                "not_multiple_of",
                args[1].bind(py).extract::<String>()?,
                params,
            )
        }
        Kind::Literal => {
            let expected = args[0].bind(py);
            let message = format!("Expected {}", literal_description(expected)?);
            let params = PyDict::new(py);
            params.set_item(
                "values",
                PyTuple::new(py, vec![safe_param(py, expected)?.unbind()])?,
            )?;
            template("invalid_value", message, params)
        }
        Kind::Enum => {
            let choices = args[0].bind(py).cast::<PyTuple>().map_err(PyErr::from)?;
            let message = enum_message(choices)?;
            let params = PyDict::new(py);
            params.set_item(
                "values",
                PyTuple::new(
                    py,
                    choices
                        .iter()
                        .map(|choice| safe_param(py, &choice).map(Bound::unbind))
                        .collect::<PyResult<Vec<_>>>()?,
                )?,
            )?;
            template("invalid_value", message, params)
        }
        Kind::String | Kind::Integer | Kind::Number | Kind::Boolean | Kind::None => {
            let name = match kind {
                Kind::String => "string",
                Kind::Integer => "integer",
                Kind::Number => "finite number",
                Kind::Boolean => "boolean",
                Kind::None => "None",
                _ => unreachable!(),
            };
            let params = PyDict::new(py);
            params.set_item("expected", name)?;
            template("invalid_type", format!("Expected {name}"), params)
        }
        _ => return Ok(None),
    };
    Ok(Some(template))
}
