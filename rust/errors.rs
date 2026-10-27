//! Validation paths, aggregated issues, and their Python/GC representation.

use std::sync::Arc;

use pyo3::class::{PyTraverseError, PyVisit};
use pyo3::exceptions::PyRecursionError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::MAX_DEPTH;

pub(crate) enum PathPart {
    Value(Py<PyAny>),
    Index(usize),
}

impl PathPart {
    pub(crate) fn clone_ref(&self, py: Python<'_>) -> Self {
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

    pub(crate) fn visit(&self, visit: &PyVisit<'_>) -> std::result::Result<(), PyTraverseError> {
        if let Self::Value(value) = self {
            visit.call(value)?;
        }
        Ok(())
    }
}

pub(crate) type Path = Vec<PathPart>;
pub(crate) type Result<T> = std::result::Result<T, Error>;

pub(crate) struct Issue {
    code: Arc<str>,
    message: Arc<str>,
    pub(crate) path: Path,
    pub(crate) branches: Vec<Vec<Issue>>,
    params: Py<PyAny>,
}

pub(crate) struct ErrorTemplate {
    pub(crate) code: Arc<str>,
    pub(crate) message: Arc<str>,
    pub(crate) params: Py<PyAny>,
}

pub(crate) enum Error {
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
    pub(crate) fn new(
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

    pub(crate) fn from_template(py: Python<'_>, template: &ErrorTemplate, path: &Path) -> Self {
        Self {
            code: template.code.clone(),
            message: template.message.clone(),
            path: path.iter().map(|part| part.clone_ref(py)).collect(),
            branches: Vec::new(),
            params: template.params.clone_ref(py),
        }
    }

    pub(crate) fn simple(py: Python<'_>, code: &str, message: &str, path: &Path) -> Self {
        Self::new(py, code, message, path, PyDict::new(py).into_any())
    }

    pub(crate) fn duplicate_key(
        py: Python<'_>,
        path: &Path,
        keys: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let params = PyDict::new(py);
        params.set_item("keys", keys)?;
        Ok(Self::new(
            py,
            "duplicate_key",
            "Multiple input aliases supplied",
            path,
            params.into_any(),
        ))
    }

    fn to_raw(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.to_raw_at(py, 0)
    }

    fn to_raw_at(&self, py: Python<'_>, depth: usize) -> PyResult<Py<PyAny>> {
        if depth >= MAX_DEPTH {
            return Err(issue_depth_error());
        }
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
                    .map(|i| i.to_raw_at(py, depth + 1))
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

    pub(crate) fn from_python(value: &Bound<'_, PyAny>) -> PyResult<Self> {
        Self::from_python_at(value, 0)
    }

    fn from_python_at(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Self> {
        if depth >= MAX_DEPTH {
            return Err(issue_depth_error());
        }
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
                    .map(|issue| Self::from_python_at(&issue?, depth + 1))
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
pub(crate) struct NativeIssueBundle {
    pub(crate) issues: Vec<Issue>,
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

fn issue_depth_error() -> PyErr {
    PyRecursionError::new_err("Issue branch depth exceeds 256 levels")
}

pub(crate) fn visit_issue(
    issue: &Issue,
    visit: &PyVisit<'_>,
) -> std::result::Result<(), PyTraverseError> {
    // Most issues have no branches; keep the common GC path allocation-free.
    if issue.branches.is_empty() {
        for part in &issue.path {
            part.visit(visit)?;
        }
        visit.call(&issue.params)?;
        return Ok(());
    }
    // Iterate with an explicit stack: GC traversal must not recurse through
    // branch graphs. The depth cap bounds a malformed Python-built cycle.
    let mut pending = vec![(issue, 0usize)];
    while let Some((issue, depth)) = pending.pop() {
        for part in &issue.path {
            part.visit(visit)?;
        }
        visit.call(&issue.params)?;
        if depth + 1 < MAX_DEPTH {
            for branch in &issue.branches {
                for child in branch {
                    pending.push((child, depth + 1));
                }
            }
        }
    }
    Ok(())
}
