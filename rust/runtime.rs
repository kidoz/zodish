//! Per-parse evaluation context and lazy schema resolution caches.

use std::cell::RefCell;
use std::sync::{Arc, Mutex};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::errors::{Error, ErrorTemplate, Issue, NativeIssueBundle, Path, Result};
use crate::schema::NativeSchema;

#[derive(Default)]
pub(crate) struct LazyState {
    pub(crate) cache: Vec<(usize, Py<NativeSchema>)>,
    last: usize,
}

pub(crate) enum LazyHandle {
    Local(RefCell<LazyState>),
    Shared(Arc<Mutex<LazyState>>),
}

impl LazyHandle {
    fn with_state<T>(&self, f: impl FnOnce(&mut LazyState) -> T) -> PyResult<T> {
        match self {
            Self::Local(state) => Ok(f(&mut state.borrow_mut())),
            Self::Shared(state) => {
                let mut guard = state
                    .lock()
                    .map_err(|_| PyRuntimeError::new_err("Lazy cache lock poisoned"))?;
                Ok(f(&mut guard))
            }
        }
    }

    pub(crate) fn resolve(
        &self,
        py: Python<'_>,
        key: usize,
        factory: &Bound<'_, PyAny>,
    ) -> PyResult<Py<NativeSchema>> {
        let cached = self.with_state(|state| {
            if !state
                .cache
                .get(state.last)
                .is_some_and(|(id, _)| *id == key)
            {
                state.last = state.cache.iter().rposition(|(id, _)| *id == key)?;
            }
            Some(state.cache[state.last].1.clone_ref(py))
        })?;
        if let Some(node) = cached {
            return Ok(node);
        }
        // Factory calls and Python finalizers must run outside the cache lock.
        let node = factory
            .call0()?
            .getattr("_node")?
            .cast_into::<NativeSchema>()?
            .unbind();
        self.with_state(|state| {
            state.last = state.cache.len();
            state.cache.push((key, node.clone_ref(py)));
        })?;
        Ok(node)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn poisoned_lazy_cache_returns_an_error() {
        let state = Arc::new(Mutex::new(LazyState::default()));
        let holder = Arc::clone(&state);
        let worker = std::thread::spawn(move || {
            let _guard = holder.lock().unwrap();
            panic!("simulate a failed cache update");
        });
        assert!(worker.join().is_err());
        assert!(LazyHandle::Shared(state).with_state(|_| ()).is_err());
    }
}
