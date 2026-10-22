//! JSON decoding and fast evaluation for supported schema/value subsets.
//! Unsupported inputs return to Python decoding and ordinary native validation.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use serde_json::Value;

use super::{Kind, NativeSchema, UnknownKeys};
use crate::MAX_DEPTH;
use crate::stack::ensure_stack;

impl NativeSchema {
    pub(super) fn validate_json_fast(
        &self,
        py: Python<'_>,
        value: &Value,
        depth: usize,
    ) -> PyResult<Option<Py<PyAny>>> {
        if depth >= MAX_DEPTH {
            return Ok(None);
        }
        ensure_stack()?;
        match self.kind {
            Kind::String => match value {
                Value::String(text) => Ok(Some(PyString::new(py, text).into_any().unbind())),
                _ => Ok(None),
            },
            Kind::Integer => match value {
                Value::Number(number) => {
                    if let Some(number) = number.as_i64() {
                        Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
                    } else if let Some(number) = number.as_u64() {
                        Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
                    } else {
                        Ok(None)
                    }
                }
                _ => Ok(None),
            },
            Kind::Boolean => match value {
                Value::Bool(boolean) => Ok(Some(
                    (*boolean).into_pyobject(py)?.to_owned().into_any().unbind(),
                )),
                _ => Ok(None),
            },
            Kind::None => {
                if value.is_null() {
                    Ok(Some(py.None()))
                } else {
                    Ok(None)
                }
            }
            Kind::Literal => {
                let expected = self.args[0].bind(py);
                if !json_literal_matches(value, expected)?.unwrap_or(false) {
                    return Ok(None);
                }
                json_scalar_to_py(py, value)
            }
            Kind::Enum => {
                let choices = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                for choice in choices.iter() {
                    match json_literal_matches(value, &choice)? {
                        Some(true) => return json_scalar_to_py(py, value),
                        Some(false) => {}
                        None => return Ok(None),
                    }
                }
                Ok(None)
            }
            Kind::Positive => {
                let positive = json_integer_i128(value).is_some_and(|number| number > 0);
                if !positive {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::Min | Kind::Max | Kind::ExclusiveMin | Kind::ExclusiveMax => {
                let Some(actual) = json_integer_i128(value) else {
                    return Ok(None);
                };
                let Some(bound) = python_int_i128(self.args[0].bind(py)) else {
                    return Ok(None);
                };
                let valid = match self.kind {
                    Kind::Min => actual >= bound,
                    Kind::Max => actual <= bound,
                    Kind::ExclusiveMin => actual > bound,
                    Kind::ExclusiveMax => actual < bound,
                    _ => unreachable!(),
                };
                if !valid {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::MultipleOf => {
                let Some(actual) = json_integer_i128(value) else {
                    return Ok(None);
                };
                let Some(divisor) = python_int_i128(self.args[0].bind(py)) else {
                    return Ok(None);
                };
                if divisor <= 0 || actual % divisor != 0 {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::Nullable => {
                if value.is_null() {
                    Ok(Some(py.None()))
                } else {
                    self.child_json_fast(py, value, depth)
                }
            }
            Kind::Optional => {
                if value.is_null() {
                    Ok(None)
                } else {
                    self.child_json_fast(py, value, depth)
                }
            }
            Kind::NonOptional | Kind::RequiredOutput | Kind::Wire => {
                self.child_json_fast(py, value, depth)
            }
            Kind::LengthMin | Kind::LengthMax => {
                let Ok(bound) = self.args[0].bind(py).extract::<usize>() else {
                    return Ok(None);
                };
                let length = match value {
                    Value::String(text) => text.chars().count(),
                    Value::Array(items) => items.len(),
                    _ => return Ok(None),
                };
                let valid = if matches!(self.kind, Kind::LengthMin) {
                    length >= bound
                } else {
                    length <= bound
                };
                if !valid {
                    return Ok(None);
                }
                self.child_json_fast(py, value, depth)
            }
            Kind::Array => {
                let Value::Array(items) = value else {
                    return Ok(None);
                };
                let mut result = Vec::with_capacity(items.len());
                for item in items {
                    let Some(parsed) = self.child_json_fast(py, item, depth)? else {
                        return Ok(None);
                    };
                    result.push(parsed);
                }
                Ok(Some(PyList::new(py, result)?.into_any().unbind()))
            }
            Kind::Object => {
                let Value::Object(map) = value else {
                    return Ok(None);
                };
                let keys = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let input_keys = self.args[3]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let result = PyDict::new(py);
                let mut present = 0;
                for (index, key) in keys.iter().enumerate() {
                    let input_key = input_keys.get_item(index)?;
                    let Ok(key_text) = input_key.cast::<PyString>().map_err(PyErr::from)?.to_str()
                    else {
                        return Ok(None);
                    };
                    let child = self.children[index].try_borrow(py).map_err(PyErr::from)?;
                    let Some(raw) = map.get(key_text) else {
                        if matches!(child.kind, Kind::Optional) {
                            continue;
                        }
                        return Ok(None);
                    };
                    present += 1;
                    let Some(parsed) = child.validate_json_fast(py, raw, depth + 1)? else {
                        return Ok(None);
                    };
                    result.set_item(&key, parsed)?;
                }
                if self.unknown_keys == UnknownKeys::Strict && present != map.len() {
                    return Ok(None);
                }
                Ok(Some(result.into_any().unbind()))
            }
            _ => Ok(None),
        }
    }

    fn child_json_fast(
        &self,
        py: Python<'_>,
        value: &Value,
        depth: usize,
    ) -> PyResult<Option<Py<PyAny>>> {
        self.children[0]
            .try_borrow(py)
            .map_err(PyErr::from)?
            .validate_json_fast(py, value, depth + 1)
    }
}

fn json_integer_i128(value: &Value) -> Option<i128> {
    let Value::Number(number) = value else {
        return None;
    };
    number
        .as_i64()
        .map(i128::from)
        .or_else(|| number.as_u64().map(i128::from))
}

fn python_int_i128(value: &Bound<'_, PyAny>) -> Option<i128> {
    value
        .extract::<i64>()
        .ok()
        .map(i128::from)
        .or_else(|| value.extract::<u64>().ok().map(i128::from))
}

fn json_scalar_to_py(py: Python<'_>, value: &Value) -> PyResult<Option<Py<PyAny>>> {
    match value {
        Value::Null => Ok(Some(py.None())),
        Value::Bool(boolean) => Ok(Some(
            (*boolean).into_pyobject(py)?.to_owned().into_any().unbind(),
        )),
        Value::Number(number) => {
            if let Some(number) = number.as_i64() {
                Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
            } else if let Some(number) = number.as_u64() {
                Ok(Some(number.into_pyobject(py)?.into_any().unbind()))
            } else {
                Ok(None)
            }
        }
        Value::String(text) => Ok(Some(PyString::new(py, text).into_any().unbind())),
        _ => Ok(None),
    }
}

fn json_literal_matches(value: &Value, expected: &Bound<'_, PyAny>) -> PyResult<Option<bool>> {
    if expected.is_exact_instance_of::<PyBool>() {
        let expected: bool = expected.extract()?;
        return Ok(Some(
            matches!(value, Value::Bool(boolean) if *boolean == expected),
        ));
    }
    if expected.is_exact_instance_of::<PyInt>() {
        let Some(actual) = json_integer_i128(value) else {
            return Ok(match value {
                Value::Number(number) if number.as_i64().is_none() && number.as_u64().is_none() => {
                    None
                }
                _ => Some(false),
            });
        };
        let Some(expected) = python_int_i128(expected) else {
            return Ok(None);
        };
        return Ok(Some(actual == expected));
    }
    if expected.is_exact_instance_of::<PyFloat>() {
        return Ok(None);
    }
    if expected.is_none() {
        return Ok(Some(value.is_null()));
    }
    if expected.is_exact_instance_of::<PyString>() {
        let expected = expected.cast::<PyString>().map_err(PyErr::from)?.to_str()?;
        return Ok(Some(
            matches!(value, Value::String(text) if text == expected),
        ));
    }
    Ok(None)
}

pub(super) fn parse_json_value(data: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
    if data.is_instance_of::<PyString>() {
        let text = data.cast::<PyString>().map_err(PyErr::from)?.to_str()?;
        Ok(serde_json::from_str(text).ok())
    } else if data.is_instance_of::<PyBytes>() {
        let bytes = data.cast::<PyBytes>().map_err(PyErr::from)?.as_bytes();
        Ok(serde_json::from_slice(bytes).ok())
    } else {
        Ok(None)
    }
}
