//! Python module registration and the shared native evaluation depth limit.
//! Schema evaluation lives in `schema`; resumable async sessions live in `session`.

mod errors;
mod runtime;
mod schema;
mod session;
mod stack;
mod values;

use pyo3::prelude::*;

use errors::NativeIssueBundle;
use schema::NativeSchema;
use session::AsyncSession;

const MAX_DEPTH: usize = 256;

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeSchema>()?;
    module.add_class::<AsyncSession>()?;
    module.add_class::<NativeIssueBundle>()?;
    module.add("BACKEND", "rust")?;
    module.add("MAX_DEPTH", MAX_DEPTH)?;
    Ok(())
}
