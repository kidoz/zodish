//! Remaining-stack guard for native recursion.
//!
//! `MAX_DEPTH` bounds schema depth within one parse, but a Python callback can
//! re-enter `NativeSchema::parse` and start a fresh depth-0 walk while outer
//! Rust frames remain on the thread stack. It also cannot adapt to small thread
//! stacks. Check the actual remaining stack at each native evaluation so both
//! cases raise `RecursionError` instead of overflowing the stack.

use pyo3::exceptions::PyRecursionError;
use pyo3::prelude::*;

/// Stack bytes kept free for the Python error path and callback frames.
const STACK_MARGIN: usize = 64 * 1024;

pub(crate) fn ensure_stack() -> PyResult<()> {
    match stacker::remaining_stack() {
        Some(remaining) if remaining < STACK_MARGIN => Err(PyRecursionError::new_err(
            "Native validation exhausted the available stack",
        )),
        _ => Ok(()),
    }
}
