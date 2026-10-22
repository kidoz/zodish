//! Shared Python value semantics for native validation and callbacks.

use pyo3::exceptions::{PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::MAX_DEPTH;
use crate::schema::Kind;
use crate::stack::ensure_stack;

pub(crate) fn literal_description(value: &Bound<'_, PyAny>) -> PyResult<String> {
    match value.repr() {
        Ok(text) => text.extract(),
        Err(error)
            if value.is_exact_instance_of::<PyInt>()
                && error.is_instance_of::<PyValueError>(value.py()) =>
        {
            let bits: usize = value.call_method0("bit_length")?.extract()?;
            Ok(format!("<integer with {bits} bits>"))
        }
        Err(error) => Err(error),
    }
}

pub(crate) fn primitive_matches(item: &Bound<'_, PyAny>, kind: Kind) -> PyResult<bool> {
    Ok(match kind {
        Kind::String => item.is_instance_of::<PyString>(),
        Kind::Integer => item.is_exact_instance_of::<PyInt>(),
        Kind::Number => {
            item.is_exact_instance_of::<PyInt>()
                || (item.is_exact_instance_of::<PyFloat>() && item.extract::<f64>()?.is_finite())
        }
        Kind::Boolean => item.is_exact_instance_of::<PyBool>(),
        Kind::None => item.is_none(),
        Kind::Unknown => true,
        _ => false,
    })
}

pub(crate) fn is_multiple_of(
    value: &Bound<'_, PyAny>,
    divisor: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if divisor.is_exact_instance_of::<PyBool>() {
        return Ok(false);
    }
    if divisor.is_exact_instance_of::<PyInt>() && divisor.eq(0)? {
        return Ok(false);
    }
    if value.is_exact_instance_of::<PyInt>() && divisor.is_exact_instance_of::<PyInt>() {
        return Ok(!value.rem(divisor)?.is_truthy()?);
    }
    if value.is_exact_instance_of::<PyInt>() && divisor.is_exact_instance_of::<PyFloat>() {
        let divisor_float: f64 = divisor.extract()?;
        if divisor_float.is_finite() && divisor_float > 0.0 && divisor_float.fract() == 0.0 {
            // `i64::MAX as f64` rounds up to 2**63, so values at or above it
            // must use an exact Python integer conversion instead of `as i64`.
            if divisor_float < i64::MAX as f64 {
                let divisor_int = (divisor_float as i64).into_pyobject(value.py())?.into_any();
                return Ok(!value.rem(&divisor_int)?.is_truthy()?);
            }
            let ratio = divisor.call_method0("as_integer_ratio")?;
            let divisor_int = ratio.get_item(0)?;
            return Ok(!value.rem(&divisor_int)?.is_truthy()?);
        }
    }

    let value_float: f64 = match value.extract() {
        Ok(value) => value,
        Err(_) => return Ok(false),
    };
    let divisor_float: f64 = match divisor.extract() {
        Ok(divisor) => divisor,
        Err(_) => return Ok(false),
    };
    if !value_float.is_finite() || !divisor_float.is_finite() || divisor_float <= 0.0 {
        return Ok(false);
    }
    let quotient = value_float / divisor_float;
    if !quotient.is_finite() {
        return Ok(false);
    }
    Ok((quotient - quotient.round()).abs() <= 8.0 * f64::EPSILON * quotient.abs().max(1.0))
}

pub(crate) fn safe_param<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    match value.str() {
        Ok(_) => Ok(value.clone()),
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            Ok(literal_description(value)?.into_pyobject(py)?.into_any())
        }
        Err(error) => Err(error),
    }
}

pub(crate) fn enum_message(choices: &Bound<'_, PyTuple>) -> PyResult<String> {
    const LIMIT: usize = 10;
    let mut parts = Vec::with_capacity(choices.len().min(LIMIT));
    for (index, choice) in choices.iter().enumerate() {
        if index == LIMIT {
            parts.push("...".to_string());
            break;
        }
        parts.push(literal_description(&choice)?);
    }
    Ok(format!("Expected one of: {}", parts.join(", ")))
}

pub(crate) fn coerce_value<'py>(
    py: Python<'py>,
    target: &str,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    match target {
        "string" => {
            if value.is_instance_of::<PyString>() {
                return Ok(Some(value.clone()));
            }
            if value.is_exact_instance_of::<PyInt>() {
                return Ok(Some(value.str()?.into_any()));
            }
            if value.is_exact_instance_of::<PyFloat>() {
                let number: f64 = value.extract()?;
                if number.is_finite() {
                    return Ok(Some(value.str()?.into_any()));
                }
            }
            Ok(None)
        }
        "number" => {
            if value.is_exact_instance_of::<PyInt>()
                || (value.is_exact_instance_of::<PyFloat>() && value.extract::<f64>()?.is_finite())
            {
                return Ok(Some(value.clone()));
            }
            if value.is_instance_of::<PyString>() {
                return coerce_number(py, value);
            }
            Ok(None)
        }
        "integer" => {
            if value.is_exact_instance_of::<PyInt>() {
                return Ok(Some(value.clone()));
            }
            if value.is_exact_instance_of::<PyFloat>() {
                let number: f64 = value.extract()?;
                if number.is_finite() && number.fract() == 0.0 {
                    let parsed = py.import("builtins")?.getattr("int")?.call1((value,))?;
                    return Ok(Some(parsed));
                }
            }
            if value.is_instance_of::<PyString>() {
                return coerce_integer(py, value);
            }
            Ok(None)
        }
        "boolean" => coerce_boolean(py, value),
        _ => Ok(None),
    }
}

fn coerce_number<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let text: String = value.call_method0("strip")?.extract()?;
    if text.is_empty() {
        return Ok(None);
    }
    let builtins = py.import("builtins")?;
    match builtins.getattr("int")?.call1((&text,)) {
        Ok(parsed) => return Ok(Some(parsed)),
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyTypeError>(py) => {}
        Err(error) => return Err(error),
    }
    match builtins.getattr("float")?.call1((&text,)) {
        Ok(parsed) => {
            let number: f64 = parsed.extract()?;
            if number.is_finite() {
                Ok(Some(parsed))
            } else {
                Ok(None)
            }
        }
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyTypeError>(py) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn coerce_integer<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let text: String = value.call_method0("strip")?.extract()?;
    if text.is_empty() {
        return Ok(None);
    }
    match py.import("builtins")?.getattr("int")?.call1((&text,)) {
        Ok(parsed) => Ok(Some(parsed)),
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyTypeError>(py) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn coerce_boolean<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if value.is_exact_instance_of::<PyBool>() {
        return Ok(Some(value.clone()));
    }
    if value.is_exact_instance_of::<PyInt>() {
        if value.eq(0)? {
            return Ok(Some(false.into_pyobject(py)?.to_owned().into_any()));
        }
        if value.eq(1)? {
            return Ok(Some(true.into_pyobject(py)?.to_owned().into_any()));
        }
        return Ok(None);
    }
    if value.is_instance_of::<PyString>() {
        let normalized: String = value
            .call_method0("strip")?
            .extract::<String>()?
            .to_lowercase();
        if matches!(normalized.as_str(), "true" | "1" | "yes" | "on") {
            return Ok(Some(true.into_pyobject(py)?.to_owned().into_any()));
        }
        if matches!(normalized.as_str(), "false" | "0" | "no" | "off") {
            return Ok(Some(false.into_pyobject(py)?.to_owned().into_any()));
        }
    }
    Ok(None)
}

pub(crate) fn is_json_value(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<bool> {
    if depth >= MAX_DEPTH {
        return Err(PyRecursionError::new_err(
            "JSON value depth exceeds 256 nodes",
        ));
    }
    ensure_stack()?;
    if value.is_none()
        || value.is_instance_of::<PyBool>()
        || value.is_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
    {
        return Ok(true);
    }
    if value.is_exact_instance_of::<PyFloat>() {
        return Ok(value.extract::<f64>()?.is_finite());
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        for item in value.try_iter()? {
            if !is_json_value(&item?, depth + 1)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if let Ok(dict) = value.cast::<PyDict>() {
        for (key, item) in dict.iter() {
            if !key.is_instance_of::<PyString>() || !is_json_value(&item, depth + 1)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    Ok(false)
}

pub(crate) fn merge_dicts<'py>(
    left: &Bound<'py, PyDict>,
    right: &Bound<'py, PyDict>,
    depth: usize,
) -> PyResult<Py<PyAny>> {
    if depth >= MAX_DEPTH {
        return Err(PyRecursionError::new_err(
            "Intersection merge depth exceeds 256 levels",
        ));
    }
    ensure_stack()?;
    let result = left.copy()?;
    for (key, right_value) in right.iter() {
        let merged = match result.get_item(&key)? {
            Some(left_value)
                if left_value.is_instance_of::<PyDict>()
                    && right_value.is_instance_of::<PyDict>() =>
            {
                merge_dicts(
                    left_value.cast::<PyDict>().map_err(PyErr::from)?,
                    right_value.cast::<PyDict>().map_err(PyErr::from)?,
                    depth + 1,
                )?
            }
            _ => right_value.unbind(),
        };
        result.set_item(key, merged)?;
    }
    Ok(result.into_any().unbind())
}

pub(crate) fn deepcopy<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import("copy")?
        .getattr("deepcopy")?
        .call1((value,))
}
