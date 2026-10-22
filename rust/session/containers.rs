//! Async container continuations grouped by object, sequence, and record traversal.
//! Frame/state definitions remain in the parent session; this module shares paths.

mod dataclass;
mod object;
mod record;
mod sequence;

use pyo3::prelude::*;

use super::clone_path;
use crate::errors::{Path, PathPart};

fn child_path(py: Python<'_>, path: &Path, part: PathPart) -> Path {
    let mut child = clone_path(py, path);
    child.push(part);
    child
}
