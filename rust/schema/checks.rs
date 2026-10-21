//! Post-child validation checks and reusable error templates shared by evaluators.

use std::net::Ipv6Addr;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString, PyTuple};

use super::{Kind, NativeSchema};
use crate::errors::{ErrorTemplate, Path, Result};
use crate::runtime::Context;
use crate::values::{enum_message, is_multiple_of, literal_description, safe_param};

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
        let valid = match self.kind {
            Kind::Transform | Kind::Overwrite => {
                return Ok(self.args[0].bind(py).call1((parsed,))?.unbind());
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
            Kind::Regex => !self.args[0]
                .bind(py)
                .call_method1("search", (parsed,))?
                .is_none(),
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
                params.set_item(
                    "origin",
                    if parsed.is_instance_of::<PyString>() {
                        "string"
                    } else {
                        "array"
                    },
                )?;
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
                params.set_item(
                    "origin",
                    if parsed.is_instance_of::<PyString>() {
                        "string"
                    } else {
                        "array"
                    },
                )?;
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
