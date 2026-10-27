//! Per-parse evaluation context and lazy schema resolution caches.

use std::cell::RefCell;
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::errors::{Error, ErrorTemplate, Issue, NativeIssueBundle, Path, Result};
use crate::schema::NativeSchema;

#[derive(Default)]
pub(crate) struct LazyState {
    pub(crate) cache: Vec<(usize, Py<NativeSchema>)>,
    pub(crate) last: Option<(usize, Py<NativeSchema>)>,
}

pub(crate) trait LazyStore {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>>;
    fn set_last(&self, key: usize, node: Py<NativeSchema>);
    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>>;
    fn push(&self, key: usize, node: Py<NativeSchema>);
}

impl LazyStore for RefCell<LazyState> {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.borrow();
        store
            .last
            .as_ref()
            .and_then(|(cached_key, node)| (*cached_key == key).then(|| node.clone_ref(py)))
    }

    fn set_last(&self, key: usize, node: Py<NativeSchema>) {
        self.borrow_mut().last = Some((key, node));
    }

    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.borrow();
        store
            .cache
            .iter()
            .rev()
            .find(|(cached_key, _)| *cached_key == key)
            .map(|(_, node)| node.clone_ref(py))
    }

    fn push(&self, key: usize, node: Py<NativeSchema>) {
        self.borrow_mut().cache.push((key, node));
    }
}

pub(crate) enum LazyHandle {
    Local(RefCell<LazyState>),
    Shared(Arc<Mutex<LazyState>>),
}

impl LazyStore for LazyHandle {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        match self {
            Self::Local(store) => store.last(py, key),
            Self::Shared(store) => store.last(py, key),
        }
    }

    fn set_last(&self, key: usize, node: Py<NativeSchema>) {
        match self {
            Self::Local(store) => store.set_last(key, node),
            Self::Shared(store) => store.set_last(key, node),
        }
    }

    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        match self {
            Self::Local(store) => store.find(py, key),
            Self::Shared(store) => store.find(py, key),
        }
    }

    fn push(&self, key: usize, node: Py<NativeSchema>) {
        match self {
            Self::Local(store) => store.push(key, node),
            Self::Shared(store) => store.push(key, node),
        }
    }
}

impl LazyStore for Mutex<LazyState> {
    fn last(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.lock().unwrap();
        store
            .last
            .as_ref()
            .and_then(|(cached_key, node)| (*cached_key == key).then(|| node.clone_ref(py)))
    }

    fn set_last(&self, key: usize, node: Py<NativeSchema>) {
        self.lock().unwrap().last = Some((key, node));
    }

    fn find(&self, py: Python<'_>, key: usize) -> Option<Py<NativeSchema>> {
        let store = self.lock().unwrap();
        store
            .cache
            .iter()
            .rev()
            .find(|(cached_key, _)| *cached_key == key)
            .map(|(_, node)| node.clone_ref(py))
    }

    fn push(&self, key: usize, node: Py<NativeSchema>) {
        self.lock().unwrap().cache.push((key, node));
    }
}

pub(crate) struct Context<'py> {
    pub(crate) py: Python<'py>,
    pub(crate) missing: Bound<'py, PyAny>,
    pub(crate) issue_type: Bound<'py, PyAny>,
    pub(crate) error_type: Bound<'py, PyAny>,
    pub(crate) callback_context: Bound<'py, PyAny>,
    pub(crate) lazy_store: LazyHandle,
}

impl<'py> Context<'py> {
    pub(crate) fn new(
        py: Python<'py>,
        missing: Bound<'py, PyAny>,
        issue_type: Bound<'py, PyAny>,
        error_type: Bound<'py, PyAny>,
        callback_context: Bound<'py, PyAny>,
    ) -> Self {
        Self {
            py,
            missing,
            issue_type,
            error_type,
            callback_context,
            lazy_store: LazyHandle::Local(RefCell::new(LazyState::default())),
        }
    }
}

impl Context<'_> {
    pub(crate) fn fail<T>(&self, code: &str, message: &str, path: &Path) -> Result<T> {
        self.fail_with(code, message, path, PyDict::new(self.py))
    }

    pub(crate) fn fail_template<T>(&self, template: &ErrorTemplate, path: &Path) -> Result<T> {
        Err(Error::Invalid(vec![Issue::from_template(
            self.py, template, path,
        )]))
    }

    pub(crate) fn fail_with<T>(
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
    pub(crate) fn normalize(&self, error: Error, path: &Path) -> Error {
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

    pub(crate) fn make_exception(&self, error: Error) -> PyErr {
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
