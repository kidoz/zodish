//! Async-aware node evaluation and continuation setup.
//! Synchronous subtrees reuse the ordinary native evaluator. Frame storage and
//! Python lifecycle remain in the parent session; execution lives in `dispatch`.

use std::sync::atomic::Ordering;

use pyo3::exceptions::{PyRecursionError, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use super::callbacks::check_awaitable;
use super::{
    ASYNC_TOKEN_COUNTER, AsyncCallbackFrame, AsyncFrame, AsyncNext, AsyncPending, AsyncPendingKind,
    AsyncSession, AsyncTask, CatchFrame, PipeFrame, PostChildFrame, RefineFrame, StringBoolFrame,
    SuperRefineFrame, clone_path,
};
use crate::MAX_DEPTH;
use crate::errors::{Error, Path};
use crate::runtime::LazyStore;
use crate::schema::{Kind, NativeSchema, select_discriminated_branch};
use crate::values::{coerce_value, deepcopy};

impl AsyncSession {
    pub(super) fn eval_node(
        &mut self,
        py: Python<'_>,
        node: Py<NativeSchema>,
        value: Py<PyAny>,
        path: Path,
        depth: usize,
    ) -> PyResult<AsyncNext> {
        if depth >= MAX_DEPTH {
            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: Err(
                    PyRecursionError::new_err("Native schema depth exceeds 256 nodes").into(),
                ),
                path,
            }));
        }
        let ctx = self.context(py)?;
        let node_ref = node.bind(py).try_borrow().map_err(PyErr::from)?;
        if !node_ref.has_async {
            let result = node_ref.validate(&ctx, value.bind(py), &mut clone_path(py, &path), depth);
            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: result,
                path,
            }));
        }
        let kind = node_ref.kind;
        let missing = value.bind(py).is(&ctx.missing);
        match kind {
            Kind::Cleared => Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: Err(PyRuntimeError::new_err("Native schema has been cleared").into()),
                path,
            })),
            Kind::AsyncRefine
            | Kind::AsyncSuperRefine
            | Kind::AsyncTransform
            | Kind::AsyncOverwrite => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames
                    .push(AsyncFrame::AsyncCallback(AsyncCallbackFrame {
                        node: node.clone_ref(py),
                        path: clone_path(py, &path),
                    }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::AsyncPreprocess => {
                let child = node_ref.children[0].clone_ref(py);
                let callback = node_ref.args[0].clone_ref(py);
                drop(node_ref);
                let result = match callback.bind(py).call1((value.bind(py),)) {
                    Ok(result) => result,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let awaitable = match check_awaitable(py, &result) {
                    Ok(awaitable) => awaitable,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(error),
                            path,
                        }));
                    }
                };
                self.token = ASYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
                self.pending = Some(AsyncPending {
                    node: node.clone_ref(py),
                    kind: AsyncPendingKind::Preprocess,
                    parsed: None,
                    child: Some(child),
                    path: clone_path(py, &path),
                    depth,
                });
                self.awaitable = Some(awaitable);
                Ok(AsyncNext::Suspend)
            }
            Kind::AsyncDefaultFactory => {
                let child = node_ref.children[0].clone_ref(py);
                if !missing {
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value,
                        path,
                        depth: depth + 1,
                    }));
                }
                let callback = node_ref.args[0].clone_ref(py);
                drop(node_ref);
                let result = match callback.bind(py).call0() {
                    Ok(result) => result,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let awaitable = match check_awaitable(py, &result) {
                    Ok(awaitable) => awaitable,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(error),
                            path,
                        }));
                    }
                };
                self.token = ASYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
                self.pending = Some(AsyncPending {
                    node: node.clone_ref(py),
                    kind: AsyncPendingKind::DefaultFactory,
                    parsed: None,
                    child: Some(child),
                    path: clone_path(py, &path),
                    depth,
                });
                self.awaitable = Some(awaitable);
                Ok(AsyncNext::Suspend)
            }
            Kind::AsyncCatch => {
                let child = node_ref.children[0].clone_ref(py);
                let fallback = node_ref.args[0].clone_ref(py);
                self.frames.push(AsyncFrame::Catch(CatchFrame {
                    node: node.clone_ref(py),
                    fallback,
                    path: clone_path(py, &path),
                    asynchronous: true,
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Optional if missing => {
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Deliver {
                    outcome: Ok(value),
                    path,
                }))
            }
            Kind::Nullable if value.bind(py).is_none() => {
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Deliver {
                    outcome: Ok(value),
                    path,
                }))
            }
            Kind::Optional | Kind::Nullable | Kind::Wire => {
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::NonOptional => {
                if missing {
                    let outcome = ctx.fail("required", "Required", &path);
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                }
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Default | Kind::DefaultFactory => {
                let child = node_ref.children[0].clone_ref(py);
                let arg = node_ref.args[0].clone_ref(py);
                drop(node_ref);
                let value = if missing {
                    if matches!(kind, Kind::Default) {
                        match deepcopy(arg.bind(py)) {
                            Ok(value) => value.unbind(),
                            Err(error) => {
                                return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                    outcome: Err(Error::Python(error)),
                                    path,
                                }));
                            }
                        }
                    } else {
                        match arg.bind(py).call0() {
                            Ok(value) => value.unbind(),
                            Err(error) => {
                                return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                    outcome: Err(Error::Python(error)),
                                    path,
                                }));
                            }
                        }
                    }
                } else {
                    value
                };
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Lazy => {
                let key = &*node_ref as *const NativeSchema as usize;
                let resolved = match ctx.lazy_store.last(py, key) {
                    Some(node) => node,
                    None => match ctx.lazy_store.find(py, key) {
                        Some(node) => {
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            node
                        }
                        None => {
                            let factory = node_ref.args[0].clone_ref(py);
                            drop(node_ref);
                            let schema = match factory.bind(py).call0() {
                                Ok(schema) => schema,
                                Err(error) => {
                                    return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                        outcome: Err(Error::Python(error)),
                                        path,
                                    }));
                                }
                            };
                            let schema = match schema.getattr("_node") {
                                Ok(schema) => schema,
                                Err(error) => {
                                    return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                        outcome: Err(Error::Python(error)),
                                        path,
                                    }));
                                }
                            };
                            let node = match schema.cast_into::<NativeSchema>() {
                                Ok(node) => node.unbind(),
                                Err(error) => {
                                    return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                        outcome: Err(Error::Python(error.into())),
                                        path,
                                    }));
                                }
                            };
                            ctx.lazy_store.push(key, node.clone_ref(py));
                            ctx.lazy_store.set_last(key, node.clone_ref(py));
                            return Ok(AsyncNext::Task(AsyncTask::Eval {
                                node,
                                value,
                                path,
                                depth: depth + 1,
                            }));
                        }
                    },
                };
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: resolved,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Catch => {
                let child = node_ref.children[0].clone_ref(py);
                let fallback = node_ref.args[0].clone_ref(py);
                self.frames.push(AsyncFrame::Catch(CatchFrame {
                    node: node.clone_ref(py),
                    fallback,
                    path: clone_path(py, &path),
                    asynchronous: false,
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Coerce => {
                if missing {
                    let child = node_ref.children[0].clone_ref(py);
                    drop(node_ref);
                    return Ok(AsyncNext::Task(AsyncTask::Eval {
                        node: child,
                        value,
                        path,
                        depth: depth + 1,
                    }));
                }
                let target: String = match node_ref.args[0].bind(py).extract() {
                    Ok(target) => target,
                    Err(error) => {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let converted = match coerce_value(py, &target, value.bind(py)) {
                    Ok(Some(value)) => value,
                    Ok(None) => {
                        let expected = format!("coercible {target}");
                        let params = PyDict::new(py);
                        if let Err(error) = params.set_item("expected", &expected) {
                            drop(node_ref);
                            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                outcome: Err(Error::Python(error)),
                                path,
                            }));
                        }
                        let outcome = ctx.fail_with(
                            "invalid_type",
                            &format!("Expected {expected}"),
                            &path,
                            params,
                        );
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver { outcome, path }));
                    }
                    Err(error) => {
                        drop(node_ref);
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value: converted.unbind(),
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Preprocess => {
                let callback = node_ref.args[0].clone_ref(py);
                let child = node_ref.children[0].clone_ref(py);
                drop(node_ref);
                let processed = match callback.bind(py).call1((value.bind(py),)) {
                    Ok(value) => value.unbind(),
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error)),
                            path,
                        }));
                    }
                };
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value: processed,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Refine => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::Refine(RefineFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::SuperRefine => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::SuperRefine(SuperRefineFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Transform
            | Kind::RequiredOutput
            | Kind::Overwrite
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
            | Kind::Cidrv4
            | Kind::Cidrv6
            | Kind::Trim
            | Kind::Lower
            | Kind::Upper => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::PostChild(PostChildFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::StringBool => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::StringBool(StringBoolFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Pipe => {
                let child = node_ref.children[0].clone_ref(py);
                self.frames.push(AsyncFrame::Pipe(PipeFrame {
                    node: node.clone_ref(py),
                    path: clone_path(py, &path),
                    depth,
                    stage: 0,
                }));
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            Kind::Array | Kind::Set | Kind::SetEncode | Kind::Tuple => {
                self.start_array(py, &ctx, node, &node_ref, value, path, depth)
            }
            Kind::Object => self.start_object(py, &ctx, node, &node_ref, value, path, depth),
            Kind::Record => self.start_record(py, &ctx, node, &node_ref, value, path, depth),
            Kind::Union => self.start_union(py, node, &node_ref, value, path, depth),
            Kind::Intersection => {
                self.start_intersection(py, &ctx, node, &node_ref, value, path, depth)
            }
            Kind::DiscriminatedUnion => {
                let key = node_ref.args[0].bind(py);
                let options = match node_ref.args[1].bind(py).cast::<PyTuple>() {
                    Ok(options) => options,
                    Err(error) => {
                        return Ok(AsyncNext::Task(AsyncTask::Deliver {
                            outcome: Err(Error::Python(error.into())),
                            path,
                        }));
                    }
                };
                let index =
                    match select_discriminated_branch(&ctx, value.bind(py), key, options, &path) {
                        Ok(index) => index,
                        Err(error) => {
                            return Ok(AsyncNext::Task(AsyncTask::Deliver {
                                outcome: Err(error),
                                path,
                            }));
                        }
                    };
                let child = node_ref.children[index].clone_ref(py);
                drop(node_ref);
                Ok(AsyncNext::Task(AsyncTask::Eval {
                    node: child,
                    value,
                    path,
                    depth: depth + 1,
                }))
            }
            _ => Ok(AsyncNext::Task(AsyncTask::Deliver {
                outcome: Err(PyTypeError::new_err(
                    "Async schema composition is not supported for this operation",
                )
                .into()),
                path,
            })),
        }
    }
}
