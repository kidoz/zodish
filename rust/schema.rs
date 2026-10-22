//! Immutable native schema nodes, Python construction, and GC integration.

mod checks;
mod evaluate;
mod json;
mod kind;

use std::collections::HashSet;

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyString, PyTuple, PyType};

use crate::errors::ErrorTemplate;
use crate::runtime::Context;

use checks::build_error_template;
use json::parse_json_value;

pub(crate) use checks::{
    evaluate_string_bool, interpret_super_refine, select_discriminated_branch,
};
pub(crate) use evaluate::is_primitive_kind;
pub(crate) use kind::Kind;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum UnknownKeys {
    Strip,
    Strict,
    Passthrough,
    Catchall,
}

#[pyclass(module = "zodish._native")]
pub(crate) struct NativeSchema {
    pub(crate) kind: Kind,
    pub(crate) children: Vec<Py<NativeSchema>>,
    pub(crate) args: Vec<Py<PyAny>>,
    pub(crate) unknown_keys: UnknownKeys,
    json_fast: bool,
    // Conservative capability bit: lazy subtrees are treated as async until
    // their factory resolves. Argument schema edges (object catchall, record
    // key, tuple rest) are included so fast-path selection cannot skip them.
    pub(crate) has_async: bool,
    error_template: Option<ErrorTemplate>,
}

#[pymethods]
impl NativeSchema {
    #[new]
    fn new(
        py: Python<'_>,
        kind: &str,
        children: Vec<Py<Self>>,
        args: Vec<Py<PyAny>>,
    ) -> PyResult<Self> {
        let kind = Kind::from_name(kind, children.len(), args.len())?;
        let mut unknown_keys = UnknownKeys::Strip;
        if matches!(kind, Kind::Object) {
            let keys = args[0].bind(py).cast::<PyTuple>()?;
            let input_keys = args[3].bind(py).cast::<PyTuple>()?;
            let policy: String = args[1].bind(py).extract()?;
            let catchall = args[2].bind(py);
            if keys.len() != children.len()
                || input_keys.len() != children.len()
                || keys.iter().any(|key| !key.is_instance_of::<PyString>())
                || input_keys
                    .iter()
                    .any(|key| !key.is_instance_of::<PyString>())
                || !matches!(
                    policy.as_str(),
                    "strip" | "strict" | "passthrough" | "catchall"
                )
                || (policy == "catchall" && !catchall.is_instance_of::<Self>())
                || (policy != "catchall" && !catchall.is_none())
            {
                return Err(PyValueError::new_err("Invalid native object shape"));
            }
            unknown_keys = match policy.as_str() {
                "strip" => UnknownKeys::Strip,
                "strict" => UnknownKeys::Strict,
                "passthrough" => UnknownKeys::Passthrough,
                "catchall" => UnknownKeys::Catchall,
                _ => unreachable!(),
            };
        }
        if matches!(kind, Kind::Record | Kind::Tuple) {
            let extra = args[0].bind(py);
            if !extra.is_none() && !extra.is_instance_of::<Self>() {
                return Err(PyValueError::new_err("Invalid native container schema"));
            }
        }
        if matches!(kind, Kind::ExactInstance | Kind::NativeEnum)
            && !args[0].bind(py).is_instance_of::<PyType>()
        {
            return Err(PyValueError::new_err("Invalid native type schema"));
        }
        if matches!(kind, Kind::Coerce) {
            let target: String = args[0].bind(py).extract()?;
            if !matches!(target.as_str(), "string" | "number" | "integer" | "boolean") {
                return Err(PyValueError::new_err("Invalid coercion target"));
            }
        }
        if matches!(kind, Kind::StringBool) {
            let truthy = args[0].bind(py).cast::<PyTuple>()?;
            let falsy = args[1].bind(py).cast::<PyTuple>()?;
            if truthy
                .iter()
                .chain(falsy.iter())
                .any(|value| !value.is_instance_of::<PyString>())
                || !args[2].bind(py).is_instance_of::<PyBool>()
            {
                return Err(PyValueError::new_err(
                    "Invalid native stringbool configuration",
                ));
            }
        }
        if matches!(kind, Kind::Enum) {
            let choices = args[0].bind(py).cast::<PyTuple>()?;
            if choices.is_empty() {
                return Err(PyValueError::new_err(
                    "Enum must contain at least one value",
                ));
            }
        }
        if matches!(kind, Kind::DiscriminatedUnion) {
            let key = args[0].bind(py);
            let options = args[1].bind(py).cast::<PyTuple>()?;
            if !key.is_instance_of::<PyString>()
                || options.is_empty()
                || options.len() != children.len()
                || options
                    .iter()
                    .any(|option| !option.is_instance_of::<PyString>())
            {
                return Err(PyValueError::new_err(
                    "Invalid native discriminated union options",
                ));
            }
            let mut seen = HashSet::new();
            for option in options.iter() {
                let option: String = option.extract()?;
                if !seen.insert(option) {
                    return Err(PyValueError::new_err(
                        "Invalid native discriminated union options",
                    ));
                }
            }
        }
        let json_fast = match kind {
            Kind::String | Kind::Integer | Kind::Boolean | Kind::None => true,
            Kind::Literal | Kind::Enum => true,
            Kind::Positive
            | Kind::LengthMin
            | Kind::LengthMax
            | Kind::Min
            | Kind::Max
            | Kind::ExclusiveMin
            | Kind::ExclusiveMax
            | Kind::MultipleOf
            | Kind::Nullable
            | Kind::Wire
            | Kind::Optional
            | Kind::NonOptional
            | Kind::RequiredOutput => children
                .first()
                .map(|child| child.try_borrow(py).map(|child| child.json_fast))
                .transpose()
                .map_err(PyErr::from)?
                .unwrap_or(false),
            Kind::Array => children[0].try_borrow(py).map_err(PyErr::from)?.json_fast,
            Kind::Object => {
                let mut all_fast = true;
                for child in &children {
                    if !child.try_borrow(py).map_err(PyErr::from)?.json_fast {
                        all_fast = false;
                        break;
                    }
                }
                matches!(unknown_keys, UnknownKeys::Strip | UnknownKeys::Strict) && all_fast
            }
            _ => false,
        };
        // Schema edges stored in arguments (object catchall, record key, tuple
        // rest) also determine async capability. Lazy factories are unknown
        // until evaluation, so treat their subtrees conservatively.
        let mut has_async = matches!(
            kind,
            Kind::AsyncRefine
                | Kind::AsyncSuperRefine
                | Kind::AsyncTransform
                | Kind::AsyncPreprocess
                | Kind::AsyncDefaultFactory
                | Kind::AsyncCatch
                | Kind::AsyncOverwrite
                | Kind::Lazy
        ) || children
            .iter()
            .any(|child| child.try_borrow(py).is_ok_and(|child| child.has_async));
        if !has_async {
            for arg in &args {
                let arg = arg.bind(py);
                if arg.is_instance_of::<Self>() {
                    if let Ok(node) = arg.extract::<PyRef<'_, Self>>() {
                        if node.has_async {
                            has_async = true;
                            break;
                        }
                    }
                }
            }
        }
        let error_template = build_error_template(py, kind, &children, &args)
            .ok()
            .flatten();
        Ok(Self {
            kind,
            children,
            args,
            unknown_keys,
            json_fast,
            has_async,
            error_template,
        })
    }

    #[getter]
    fn kind(&self) -> &'static str {
        self.kind.name()
    }

    #[getter]
    fn has_async(&self) -> bool {
        self.has_async
    }

    #[getter]
    fn children(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.children.iter().map(|child| child.clone_ref(py)))?.unbind())
    }

    #[getter]
    fn args(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.args.iter().map(|arg| arg.clone_ref(py)))?.unbind())
    }

    pub(crate) fn parse<'py>(
        &self,
        py: Python<'py>,
        value: Bound<'py, PyAny>,
        missing: Bound<'py, PyAny>,
        issue_type: Bound<'py, PyAny>,
        error_type: Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let ctx = Context::new(py, missing, issue_type, error_type);
        self.validate(&ctx, &value, &mut Vec::new(), 0)
            .map_err(|error| ctx.make_exception(error))
    }

    fn parse_json(&self, py: Python<'_>, data: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let parsed = if self.json_fast {
            match parse_json_value(&data)? {
                Some(value) => self.validate_json_fast(py, &value, 0)?,
                None => None,
            }
        } else {
            None
        };
        match parsed {
            Some(value) => Ok((true, value).into_pyobject(py)?.into_any().unbind()),
            None => Ok((false, py.None()).into_pyobject(py)?.into_any().unbind()),
        }
    }

    // Lazy factories may close over a Python schema which points back to this
    // native node. Participate in Python GC so those cycles are collectible.
    fn __traverse__(&self, visit: PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        for child in &self.children {
            visit.call(child)?;
        }
        for arg in &self.args {
            visit.call(arg)?;
        }
        if let Some(template) = &self.error_template {
            visit.call(&template.params)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.kind = Kind::Cleared;
        self.children.clear();
        self.args.clear();
        self.error_template = None;
    }
}
