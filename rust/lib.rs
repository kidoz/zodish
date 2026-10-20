//! Native schema evaluation. Python builds immutable nodes; all recursive
//! traversal, built-in checks, and issue aggregation are performed here.
//! Values stay as Python objects to preserve arbitrary precision and identity.

use std::cell::RefCell;
use std::collections::HashSet;
use std::net::Ipv6Addr;
use std::sync::Arc;

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyRecursionError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple, PyType};
use serde_json::Value;

const MAX_DEPTH: usize = 256;

enum PathPart {
    Value(Py<PyAny>),
    Index(usize),
}

impl PathPart {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Value(value) => Self::Value(value.clone_ref(py)),
            Self::Index(index) => Self::Index(*index),
        }
    }

    fn to_py(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::Value(value) => Ok(value.clone_ref(py)),
            Self::Index(index) => Ok((*index).into_pyobject(py)?.into_any().unbind()),
        }
    }

    fn visit(&self, visit: &PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        if let Self::Value(value) = self {
            visit.call(value)?;
        }
        Ok(())
    }
}

type Path = Vec<PathPart>;
type Result<T> = std::result::Result<T, Error>;

struct Issue {
    code: Arc<str>,
    message: Arc<str>,
    path: Path,
    branches: Vec<Vec<Issue>>,
    params: Py<PyAny>,
}

struct ErrorTemplate {
    code: Arc<str>,
    message: Arc<str>,
    params: Py<PyAny>,
}

enum Error {
    Invalid(Vec<Issue>),
    Python(PyErr),
}

impl From<PyErr> for Error {
    fn from(error: PyErr) -> Self {
        Self::Python(error)
    }
}

impl From<std::convert::Infallible> for Error {
    fn from(never: std::convert::Infallible) -> Self {
        match never {}
    }
}

impl Issue {
    fn new(
        py: Python<'_>,
        code: &str,
        message: &str,
        path: &Path,
        params: Bound<'_, PyAny>,
    ) -> Self {
        Self {
            code: Arc::from(code),
            message: Arc::from(message),
            path: path.iter().map(|part| part.clone_ref(py)).collect(),
            branches: Vec::new(),
            params: params.unbind(),
        }
    }

    fn from_template(py: Python<'_>, template: &ErrorTemplate, path: &Path) -> Self {
        Self {
            code: template.code.clone(),
            message: template.message.clone(),
            path: path.iter().map(|part| part.clone_ref(py)).collect(),
            branches: Vec::new(),
            params: template.params.clone_ref(py),
        }
    }

    fn simple(py: Python<'_>, code: &str, message: &str, path: &Path) -> Self {
        Self::new(py, code, message, path, PyDict::new(py).into_any())
    }

    fn to_raw(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let path = PyTuple::new(
            py,
            self.path
                .iter()
                .map(|part| part.to_py(py))
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let branches = self
            .branches
            .iter()
            .map(|branch| {
                let issues = branch
                    .iter()
                    .map(|i| i.to_raw(py))
                    .collect::<PyResult<Vec<_>>>()?;
                Ok(PyTuple::new(py, issues)?.into_any().unbind())
            })
            .collect::<PyResult<Vec<Py<PyAny>>>>()?;
        Ok((
            self.code.as_ref(),
            self.message.as_ref(),
            path,
            PyTuple::new(py, branches)?,
            self.params.bind(py),
        )
            .into_pyobject(py)?
            .into_any()
            .unbind())
    }

    fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        let path = value
            .getattr("path")?
            .try_iter()?
            .map(|v| v.map(|part| PathPart::Value(part.unbind())))
            .collect::<PyResult<Vec<_>>>()?;
        let branches = value
            .getattr("branches")?
            .try_iter()?
            .map(|branch| {
                branch?
                    .try_iter()?
                    .map(|issue| Self::from_python(&issue?))
                    .collect::<PyResult<Vec<_>>>()
            })
            .collect::<PyResult<Vec<_>>>()?;
        let params = value
            .getattr("params")
            .unwrap_or_else(|_| PyDict::new(value.py()).into_any())
            .unbind();
        Ok(Self {
            code: Arc::from(value.getattr("code")?.extract::<String>()?),
            message: Arc::from(value.getattr("message")?.extract::<String>()?),
            path,
            branches,
            params,
        })
    }
}

#[pyclass(module = "zodish._native")]
struct NativeIssueBundle {
    issues: Vec<Issue>,
}

#[pymethods]
impl NativeIssueBundle {
    fn to_raw(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let issues = self
            .issues
            .iter()
            .map(|issue| issue.to_raw(py))
            .collect::<PyResult<Vec<_>>>()?;
        Ok(PyTuple::new(py, issues)?.into_any().unbind())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        for issue in &self.issues {
            visit_issue(issue, &visit)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.issues.clear();
    }
}

fn visit_issue(issue: &Issue, visit: &PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
    for part in &issue.path {
        part.visit(visit)?;
    }
    visit.call(&issue.params)?;
    for branch in &issue.branches {
        for child in branch {
            visit_issue(child, visit)?;
        }
    }
    Ok(())
}

struct Context<'py> {
    py: Python<'py>,
    missing: Bound<'py, PyAny>,
    issue_type: Bound<'py, PyAny>,
    error_type: Bound<'py, PyAny>,
    lazy_cache: RefCell<Vec<(usize, Py<NativeSchema>)>>,
    lazy_last: RefCell<Option<(usize, Py<NativeSchema>)>>,
}

impl Context<'_> {
    fn fail<T>(&self, code: &str, message: &str, path: &Path) -> Result<T> {
        self.fail_with(code, message, path, PyDict::new(self.py))
    }

    fn fail_template<T>(&self, template: &ErrorTemplate, path: &Path) -> Result<T> {
        Err(Error::Invalid(vec![Issue::from_template(
            self.py, template, path,
        )]))
    }

    fn fail_with<T>(
        &self,
        code: &str,
        message: &str,
        path: &Path,
        params: Bound<'_, PyDict>,
    ) -> Result<T> {
        Err(Error::Invalid(vec![Issue::new(
            self.py,
            code,
            message,
            path,
            params.into_any(),
        )]))
    }

    // A Python callback can deliberately raise our public ValidationError.
    // Normalize it so its issues also aggregate with sibling failures.
    fn normalize(&self, error: Error, path: &Path) -> Error {
        if let Error::Python(error) = error {
            if error
                .matches(self.py, &self.error_type)
                .is_ok_and(|matched| matched)
            {
                let converted = (|| {
                    error
                        .value(self.py)
                        .getattr("issues")?
                        .try_iter()?
                        .map(|issue| Issue::from_python(&issue?))
                        .collect::<PyResult<Vec<_>>>()
                })();
                return match converted {
                    // A rejection with no details must still fail and aggregate.
                    Ok(issues) if issues.is_empty() => Error::Invalid(vec![Issue::simple(
                        self.py,
                        "custom",
                        "Invalid value",
                        path,
                    )]),
                    Ok(issues) => Error::Invalid(issues),
                    Err(error) => Error::Python(error),
                };
            }
            Error::Python(error)
        } else {
            error
        }
    }

    fn make_exception(&self, error: Error) -> PyErr {
        match error {
            Error::Python(error) => error,
            Error::Invalid(issues) => {
                let exception = (move || {
                    let bundle = Py::new(self.py, NativeIssueBundle { issues })?;
                    self.error_type.call_method1("_from_bundle", (bundle,))
                })();
                match exception {
                    Ok(value) => PyErr::from_value(value),
                    Err(error) => error,
                }
            }
        }
    }
}

#[derive(Clone, Copy)]
enum Kind {
    String,
    Number,
    Integer,
    Boolean,
    Unknown,
    None,
    ExactInstance,
    Json,
    NativeEnum,
    Literal,
    Enum,
    Instance,
    Array,
    Object,
    Union,
    DiscriminatedUnion,
    Intersection,
    Record,
    Tuple,
    Lazy,
    Optional,
    NonOptional,
    Nullable,
    Default,
    DefaultFactory,
    Refine,
    SuperRefine,
    Transform,
    Overwrite,
    Catch,
    Coerce,
    StringBool,
    Preprocess,
    Pipe,
    LengthMin,
    LengthMax,
    Min,
    Max,
    ExclusiveMin,
    ExclusiveMax,
    MultipleOf,
    Positive,
    Integral,
    Regex,
    StartsWith,
    EndsWith,
    Includes,
    Ipv6,
    Trim,
    Lower,
    Upper,
    Cleared,
}

impl Kind {
    fn name(&self) -> &'static str {
        match self {
            Self::String => "string",
            Self::Number => "number",
            Self::Integer => "integer",
            Self::Boolean => "boolean",
            Self::Unknown => "unknown",
            Self::None => "none",
            Self::ExactInstance => "exact_instance",
            Self::Json => "json",
            Self::NativeEnum => "native_enum",
            Self::Literal => "literal",
            Self::Enum => "enum",
            Self::Instance => "instance",
            Self::Array => "array",
            Self::Object => "object",
            Self::Union => "union",
            Self::DiscriminatedUnion => "discriminated_union",
            Self::Intersection => "intersection",
            Self::Record => "record",
            Self::Tuple => "tuple",
            Self::Lazy => "lazy",
            Self::Optional => "optional",
            Self::NonOptional => "nonoptional",
            Self::Nullable => "nullable",
            Self::Default => "default",
            Self::DefaultFactory => "default_factory",
            Self::Refine => "refine",
            Self::SuperRefine => "super_refine",
            Self::Transform => "transform",
            Self::Overwrite => "overwrite",
            Self::Catch => "catch",
            Self::Coerce => "coerce",
            Self::StringBool => "string_bool",
            Self::Preprocess => "preprocess",
            Self::Pipe => "pipe",
            Self::LengthMin => "length_min",
            Self::LengthMax => "length_max",
            Self::Min => "min",
            Self::Max => "max",
            Self::ExclusiveMin => "exclusive_min",
            Self::ExclusiveMax => "exclusive_max",
            Self::MultipleOf => "multiple_of",
            Self::Positive => "positive",
            Self::Integral => "integral",
            Self::Regex => "regex",
            Self::StartsWith => "starts_with",
            Self::EndsWith => "ends_with",
            Self::Includes => "includes",
            Self::Ipv6 => "ipv6",
            Self::Trim => "trim",
            Self::Lower => "lower",
            Self::Upper => "upper",
            Self::Cleared => "cleared",
        }
    }

    fn from_name(name: &str, children: usize, args: usize) -> PyResult<Self> {
        // Validate arity before any node is accepted; evaluation never indexes
        // unchecked user-supplied vectors.
        let (kind, expected_children, expected_args) = match name {
            "string" => (Self::String, Some(0), 0),
            "number" => (Self::Number, Some(0), 0),
            "integer" => (Self::Integer, Some(0), 0),
            "boolean" => (Self::Boolean, Some(0), 0),
            "unknown" => (Self::Unknown, Some(0), 0),
            "none" => (Self::None, Some(0), 0),
            "exact_instance" => (Self::ExactInstance, Some(0), 1),
            "json" => (Self::Json, Some(0), 0),
            "native_enum" => (Self::NativeEnum, Some(0), 1),
            "literal" => (Self::Literal, Some(0), 1),
            "enum" => (Self::Enum, Some(0), 1),
            "instance" => (Self::Instance, Some(0), 1),
            "array" => (Self::Array, Some(1), 0),
            "object" => (Self::Object, None, 3),
            "union" if children > 0 => (Self::Union, None, 0),
            "discriminated_union" if children > 0 => (Self::DiscriminatedUnion, None, 2),
            "intersection" => (Self::Intersection, Some(2), 0),
            "record" => (Self::Record, Some(1), 1),
            "tuple" => (Self::Tuple, None, 1),
            "lazy" => (Self::Lazy, Some(0), 1),
            "optional" => (Self::Optional, Some(1), 0),
            "nonoptional" => (Self::NonOptional, Some(1), 0),
            "nullable" => (Self::Nullable, Some(1), 0),
            "default" => (Self::Default, Some(1), 1),
            "default_factory" => (Self::DefaultFactory, Some(1), 1),
            "refine" => (Self::Refine, Some(1), 3),
            "super_refine" => (Self::SuperRefine, Some(1), 1),
            "transform" => (Self::Transform, Some(1), 1),
            "overwrite" => (Self::Overwrite, Some(1), 1),
            "catch" => (Self::Catch, Some(1), 1),
            "coerce" => (Self::Coerce, Some(1), 1),
            "string_bool" => (Self::StringBool, Some(1), 4),
            "preprocess" => (Self::Preprocess, Some(1), 1),
            "pipe" => (Self::Pipe, Some(2), 0),
            "length_min" => (Self::LengthMin, Some(1), 2),
            "length_max" => (Self::LengthMax, Some(1), 2),
            "min" => (Self::Min, Some(1), 2),
            "max" => (Self::Max, Some(1), 2),
            "exclusive_min" => (Self::ExclusiveMin, Some(1), 2),
            "exclusive_max" => (Self::ExclusiveMax, Some(1), 2),
            "multiple_of" => (Self::MultipleOf, Some(1), 2),
            "positive" => (Self::Positive, Some(1), 1),
            "integral" => (Self::Integral, Some(1), 1),
            "regex" => (Self::Regex, Some(1), 3),
            "starts_with" => (Self::StartsWith, Some(1), 2),
            "ends_with" => (Self::EndsWith, Some(1), 2),
            "includes" => (Self::Includes, Some(1), 2),
            "ipv6" => (Self::Ipv6, Some(1), 1),
            "trim" => (Self::Trim, Some(1), 0),
            "lower" => (Self::Lower, Some(1), 0),
            "upper" => (Self::Upper, Some(1), 0),
            _ => {
                return Err(PyValueError::new_err(format!(
                    "Invalid native operation: {name}"
                )));
            }
        };
        if expected_children.is_some_and(|count| children != count) || args != expected_args {
            return Err(PyValueError::new_err("Invalid native schema arity"));
        }
        Ok(kind)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum UnknownKeys {
    Strip,
    Strict,
    Passthrough,
    Catchall,
}

#[pyclass(module = "zodish._native")]
struct NativeSchema {
    kind: Kind,
    children: Vec<Py<NativeSchema>>,
    args: Vec<Py<PyAny>>,
    unknown_keys: UnknownKeys,
    json_fast: bool,
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
            let policy: String = args[1].bind(py).extract()?;
            let catchall = args[2].bind(py);
            if keys.len() != children.len()
                || keys.iter().any(|key| !key.is_instance_of::<PyString>())
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
            | Kind::Optional
            | Kind::NonOptional => children
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
        let error_template = build_error_template(py, kind, &children, &args)
            .ok()
            .flatten();
        Ok(Self {
            kind,
            children,
            args,
            unknown_keys,
            json_fast,
            error_template,
        })
    }

    #[getter]
    fn kind(&self) -> &'static str {
        self.kind.name()
    }

    #[getter]
    fn children(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.children.iter().map(|child| child.clone_ref(py)))?.unbind())
    }

    #[getter]
    fn args(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(PyTuple::new(py, self.args.iter().map(|arg| arg.clone_ref(py)))?.unbind())
    }

    fn parse<'py>(
        &self,
        py: Python<'py>,
        value: Bound<'py, PyAny>,
        missing: Bound<'py, PyAny>,
        issue_type: Bound<'py, PyAny>,
        error_type: Bound<'py, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let ctx = Context {
            py,
            missing,
            issue_type,
            error_type,
            lazy_cache: RefCell::new(Vec::new()),
            lazy_last: RefCell::new(None),
        };
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

impl NativeSchema {
    fn validate(
        &self,
        ctx: &Context<'_>,
        value: &Bound<'_, PyAny>,
        path: &mut Path,
        depth: usize,
    ) -> Result<Py<PyAny>> {
        if depth >= MAX_DEPTH {
            return Err(PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into());
        }
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

    fn validate_json_fast(
        &self,
        py: Python<'_>,
        value: &Value,
        depth: usize,
    ) -> PyResult<Option<Py<PyAny>>> {
        if depth >= MAX_DEPTH {
            return Ok(None);
        }
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
            Kind::NonOptional => self.child_json_fast(py, value, depth),
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
                let result = PyDict::new(py);
                let mut present = 0;
                for (index, key) in keys.iter().enumerate() {
                    let Ok(key_text) = key.cast::<PyString>().map_err(PyErr::from)?.to_str() else {
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
            Kind::Optional if missing => keep(),
            Kind::Nullable if value.is_none() => keep(),
            Kind::Optional | Kind::Nullable => self.child(0, ctx, value, path, depth),
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
                let cached_by_last = {
                    let last = ctx.lazy_last.borrow();
                    last.as_ref().and_then(|(cached_key, node)| {
                        (*cached_key == key).then(|| node.clone_ref(py))
                    })
                };
                let node = match cached_by_last {
                    Some(node) => node,
                    None => {
                        let cached = ctx
                            .lazy_cache
                            .borrow()
                            .iter()
                            .rev()
                            .find(|(cached_key, _)| *cached_key == key)
                            .map(|(_, node)| node.clone_ref(py));
                        match cached {
                            Some(node) => {
                                *ctx.lazy_last.borrow_mut() = Some((key, node.clone_ref(py)));
                                node
                            }
                            None => {
                                let schema = self.args[0].bind(py).call0()?.getattr("_node")?;
                                let node: Py<NativeSchema> = schema
                                    .cast_into::<NativeSchema>()
                                    .map_err(PyErr::from)?
                                    .unbind();
                                ctx.lazy_cache.borrow_mut().push((key, node.clone_ref(py)));
                                *ctx.lazy_last.borrow_mut() = Some((key, node.clone_ref(py)));
                                node
                            }
                        }
                    }
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
                if result.is_none() {
                    return Ok(parsed);
                }
                let mut issues = Vec::new();
                if result.is_instance(&ctx.issue_type)? {
                    issues.push(Issue::from_python(&result)?);
                } else if result.is_instance_of::<PyList>() || result.is_instance_of::<PyTuple>() {
                    for item in result.try_iter()? {
                        let item = item?;
                        if !item.is_instance(&ctx.issue_type)? {
                            return Err(PyTypeError::new_err(
                                "super_refine callback returned a non-Issue value",
                            )
                            .into());
                        }
                        issues.push(Issue::from_python(&item)?);
                    }
                } else {
                    return Err(PyTypeError::new_err(
                        "super_refine callback must return Issue, a list or tuple of Issue, or None",
                    )
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
            Kind::Transform
            | Kind::Overwrite
            | Kind::Pipe
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
            | Kind::Trim
            | Kind::Lower
            | Kind::Upper => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                let valid = match self.kind {
                    Kind::Transform => return Ok(self.args[0].bind(py).call1((parsed,))?.unbind()),
                    Kind::Overwrite => {
                        return Ok(self.args[0].bind(py).call1((parsed,))?.unbind());
                    }
                    Kind::Pipe => return self.child(1, ctx, parsed, path, depth),
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
                        parsed.is_exact_instance_of::<PyInt>()
                            || parsed.extract::<f64>()?.fract() == 0.0
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
            _ if missing && !matches!(self.kind, Kind::Union) => {
                ctx.fail("required", "Required", path)
            }
            Kind::StringBool => {
                let parsed = self.child(0, ctx, value, path, depth)?;
                let parsed = parsed.bind(py);
                let text: String = parsed.extract()?;
                let case_sensitive: bool = self.args[2].bind(py).extract()?;
                let normalized = if case_sensitive {
                    text
                } else {
                    text.to_lowercase()
                };
                for item in self.args[0].bind(py).try_iter()? {
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
                for item in self.args[1].bind(py).try_iter()? {
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
                let message: String = self.args[3].bind(py).extract()?;
                let truthy = self.args[0]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let falsy = self.args[1]
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
            Kind::Array | Kind::Tuple => {
                let is_tuple = matches!(self.kind, Kind::Tuple);
                if !value.is_instance_of::<PyList>()
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
                let exact_list = if !is_tuple && value.is_exact_instance_of::<PyList>() {
                    Some(value.cast::<PyList>().map_err(PyErr::from)?)
                } else {
                    None
                };
                if let Some(list) = &exact_list {
                    let child_kind = self.children[0].try_borrow(py).map_err(PyErr::from)?.kind;
                    if is_primitive_kind(child_kind) {
                        if !list.is_empty() && depth + 1 >= MAX_DEPTH {
                            return Err(PyRecursionError::new_err(
                                "Native schema depth exceeds 256 nodes",
                            )
                            .into());
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
                    return Err(
                        PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into(),
                    );
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
                if is_tuple {
                    Ok(PyTuple::new(py, result)?.into_any().unbind())
                } else {
                    Ok(PyList::new(py, result)?.into_any().unbind())
                }
            }
            Kind::Object | Kind::Record => {
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
                    const INLINE_VALUES: usize = 4;
                    let policy = self.unknown_keys;
                    let mut inline_values: [Option<Bound<'_, PyAny>>; INLINE_VALUES] =
                        std::array::from_fn(|_| None);
                    let mut heap_values =
                        Vec::with_capacity(keys.len().saturating_sub(INLINE_VALUES));
                    let mut present = 0;
                    for (index, key) in keys.iter().enumerate() {
                        let value = dict.get_item(&key)?;
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
                        path.push(PathPart::Value(key.clone().unbind()));
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
                        for key in keys.iter() {
                            known.set_item(&key, true)?;
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
                                    params.set_item(
                                        "keys",
                                        PyTuple::new(py, vec![key.clone().unbind()])?,
                                    )?;
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
                                    result.set_item(key, deepcopy(&raw)?)?;
                                } else if policy == UnknownKeys::Catchall {
                                    let catchall = self.args[2].bind(py);
                                    let node: PyRef<'_, Self> =
                                        catchall.extract().map_err(PyErr::from)?;
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
            Kind::DiscriminatedUnion => {
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
                let key = self.args[0].bind(py);
                let options = self.args[1]
                    .bind(py)
                    .cast::<PyTuple>()
                    .map_err(PyErr::from)?;
                let Some(raw) = dict.get_item(key)? else {
                    path.push(PathPart::Value(self.args[0].clone_ref(py)));
                    let result = ctx.fail("required", "Required", path);
                    path.pop();
                    return result;
                };
                let mut selected = None;
                for (index, option) in options.iter().enumerate() {
                    if raw.get_type().is(option.get_type()) && raw.eq(&option)? {
                        selected = Some(index);
                        break;
                    }
                }
                let Some(index) = selected else {
                    let message = enum_message(options)?;
                    let params = PyDict::new(py);
                    params.set_item("values", options)?;
                    path.push(PathPart::Value(self.args[0].clone_ref(py)));
                    let result = ctx.fail_with("invalid_value", &message, path, params);
                    path.pop();
                    return result;
                };
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

fn literal_description(value: &Bound<'_, PyAny>) -> PyResult<String> {
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

fn is_primitive_kind(kind: Kind) -> bool {
    matches!(
        kind,
        Kind::String | Kind::Integer | Kind::Number | Kind::Boolean | Kind::None | Kind::Unknown
    )
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

fn build_error_template(
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

fn parse_json_value(data: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
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

fn primitive_matches(item: &Bound<'_, PyAny>, kind: Kind) -> PyResult<bool> {
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

fn is_multiple_of(value: &Bound<'_, PyAny>, divisor: &Bound<'_, PyAny>) -> PyResult<bool> {
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
        if divisor_float.is_finite()
            && divisor_float > 0.0
            && divisor_float.fract() == 0.0
            && divisor_float <= i64::MAX as f64
        {
            let divisor_int = (divisor_float as i64).into_pyobject(value.py())?.into_any();
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

fn safe_param<'py>(py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    match value.str() {
        Ok(_) => Ok(value.clone()),
        Err(error) if error.is_instance_of::<PyValueError>(py) => {
            Ok(literal_description(value)?.into_pyobject(py)?.into_any())
        }
        Err(error) => Err(error),
    }
}

fn enum_message(choices: &Bound<'_, PyTuple>) -> PyResult<String> {
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

fn coerce_value<'py>(
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

fn is_json_value(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<bool> {
    if depth >= MAX_DEPTH {
        return Err(PyRecursionError::new_err(
            "JSON value depth exceeds 256 nodes",
        ));
    }
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

fn merge_dicts<'py>(left: &Bound<'py, PyDict>, right: &Bound<'py, PyDict>) -> PyResult<Py<PyAny>> {
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
                )?
            }
            _ => right_value.unbind(),
        };
        result.set_item(key, merged)?;
    }
    Ok(result.into_any().unbind())
}

fn deepcopy<'py>(value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    value
        .py()
        .import("copy")?
        .getattr("deepcopy")?
        .call1((value,))
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeSchema>()?;
    module.add("BACKEND", "rust")?;
    module.add("MAX_DEPTH", MAX_DEPTH)?;
    Ok(())
}
