//! Native operation names, arity, and capability metadata.
//!
//! `define_kinds!` is the single Rust source for the `Kind` enum, operation
//! names, construction-time arity, and the capability flags used by the
//! evaluator. Add a new kind here; Python-side builders and export logic still
//! need their own entries.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// Accepted child count at construction time.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum ChildArity {
    Exact(usize),
    AtLeastOne,
    Any,
}

/// How JSON fast-path support is derived for a kind.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum JsonFast {
    Always,
    InheritFirstChild,
    ObjectFields,
    Never,
}

pub(crate) struct KindSpec {
    pub(crate) kind: Kind,
    pub(crate) name: &'static str,
    pub(crate) children: ChildArity,
    pub(crate) args: usize,
    pub(crate) async_leaf: bool,
    pub(crate) primitive: bool,
    pub(crate) json_fast: JsonFast,
}

macro_rules! define_kinds {
    ($( $variant:ident => ($name:literal, $children:expr, $args:literal, $async_leaf:literal, $primitive:literal, $json_fast:expr $(,)?) ),* $(,)?) => {
        #[derive(Clone, Copy, PartialEq, Eq)]
        pub(crate) enum Kind {
            $($variant,)*
        }

        const KIND_SPECS: &[KindSpec] = &[
            $(KindSpec {
                kind: Kind::$variant,
                name: $name,
                children: $children,
                args: $args,
                async_leaf: $async_leaf,
                primitive: $primitive,
                json_fast: $json_fast,
            },)*
        ];

        impl Kind {
            pub(crate) fn spec(self) -> &'static KindSpec {
                let spec = &KIND_SPECS[self as usize];
                debug_assert!(spec.kind == self, "kind registry order is stale");
                spec
            }

            pub(crate) fn name(self) -> &'static str {
                self.spec().name
            }

            pub(crate) fn from_name(
                name: &str,
                children: usize,
                args: usize,
            ) -> PyResult<Self> {
                let Some(spec) = KIND_SPECS.iter().find(|spec| spec.name == name) else {
                    return Err(PyValueError::new_err(format!(
                        "Invalid native operation: {name}"
                    )));
                };
                if spec.kind == Kind::Cleared {
                    return Err(PyValueError::new_err(format!(
                        "Invalid native operation: {name}"
                    )));
                }
                let children_ok = match spec.children {
                    ChildArity::Exact(count) => children == count,
                    ChildArity::AtLeastOne => children > 0,
                    ChildArity::Any => true,
                };
                if !children_ok || args != spec.args {
                    return Err(PyValueError::new_err("Invalid native schema arity"));
                }
                Ok(spec.kind)
            }
        }
    };
}

define_kinds! {
    // variant => (name, children, args, async_leaf, primitive, json_fast)
    String => ("string", ChildArity::Exact(0), 0, false, true, JsonFast::Always),
    Number => ("number", ChildArity::Exact(0), 0, false, true, JsonFast::Never),
    Integer => ("integer", ChildArity::Exact(0), 0, false, true, JsonFast::Always),
    Boolean => ("boolean", ChildArity::Exact(0), 0, false, true, JsonFast::Always),
    Unknown => ("unknown", ChildArity::Exact(0), 0, false, true, JsonFast::Never),
    None => ("none", ChildArity::Exact(0), 0, false, true, JsonFast::Always),
    ExactInstance => ("exact_instance", ChildArity::Exact(0), 1, false, false, JsonFast::Never),
    Json => ("json", ChildArity::Exact(0), 0, false, false, JsonFast::Never),
    NativeEnum => ("native_enum", ChildArity::Exact(0), 1, false, false, JsonFast::Never),
    Literal => ("literal", ChildArity::Exact(0), 1, false, false, JsonFast::Always),
    Enum => ("enum", ChildArity::Exact(0), 1, false, false, JsonFast::Always),
    Instance => ("instance", ChildArity::Exact(0), 1, false, false, JsonFast::Never),
    Array => ("array", ChildArity::Exact(1), 0, false, false, JsonFast::InheritFirstChild),
    Set => ("set", ChildArity::Exact(1), 0, false, false, JsonFast::Never),
    SetEncode => ("set_encode", ChildArity::Exact(1), 0, false, false, JsonFast::Never),
    Object => ("object", ChildArity::Any, 4, false, false, JsonFast::ObjectFields),
    Union => ("union", ChildArity::AtLeastOne, 0, false, false, JsonFast::Never),
    DiscriminatedUnion => ("discriminated_union", ChildArity::AtLeastOne, 2, false, false, JsonFast::Never),
    Intersection => ("intersection", ChildArity::Exact(2), 0, false, false, JsonFast::Never),
    Record => ("record", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Tuple => ("tuple", ChildArity::Any, 1, false, false, JsonFast::Never),
    Lazy => ("lazy", ChildArity::Exact(0), 1, true, false, JsonFast::Never),
    Optional => ("optional", ChildArity::Exact(1), 0, false, false, JsonFast::InheritFirstChild),
    NonOptional => ("nonoptional", ChildArity::Exact(1), 0, false, false, JsonFast::InheritFirstChild),
    RequiredOutput => ("required_output", ChildArity::Exact(1), 0, false, false, JsonFast::InheritFirstChild),
    Nullable => ("nullable", ChildArity::Exact(1), 0, false, false, JsonFast::InheritFirstChild),
    Wire => ("wire", ChildArity::Exact(1), 1, false, false, JsonFast::InheritFirstChild),
    Default => ("default", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    DefaultFactory => ("default_factory", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Refine => ("refine", ChildArity::Exact(1), 3, false, false, JsonFast::Never),
    SuperRefine => ("super_refine", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Transform => ("transform", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    AsyncRefine => ("async_refine", ChildArity::Exact(1), 3, true, false, JsonFast::Never),
    AsyncSuperRefine => ("async_super_refine", ChildArity::Exact(1), 1, true, false, JsonFast::Never),
    AsyncTransform => ("async_transform", ChildArity::Exact(1), 1, true, false, JsonFast::Never),
    AsyncPreprocess => ("async_preprocess", ChildArity::Exact(1), 1, true, false, JsonFast::Never),
    AsyncDefaultFactory => ("async_default_factory", ChildArity::Exact(1), 1, true, false, JsonFast::Never),
    AsyncCatch => ("async_catch", ChildArity::Exact(1), 1, true, false, JsonFast::Never),
    AsyncOverwrite => ("async_overwrite", ChildArity::Exact(1), 1, true, false, JsonFast::Never),
    Overwrite => ("overwrite", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Catch => ("catch", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Coerce => ("coerce", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    StringBool => ("string_bool", ChildArity::Exact(1), 4, false, false, JsonFast::Never),
    Preprocess => ("preprocess", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    OutputGuard => ("output_guard", ChildArity::Exact(2), 2, false, false, JsonFast::Never),
    Pipe => ("pipe", ChildArity::Exact(2), 0, false, false, JsonFast::Never),
    LengthMin => ("length_min", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    LengthMax => ("length_max", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    Min => ("min", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    Max => ("max", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    ExclusiveMin => ("exclusive_min", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    ExclusiveMax => ("exclusive_max", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    MultipleOf => ("multiple_of", ChildArity::Exact(1), 2, false, false, JsonFast::InheritFirstChild),
    Positive => ("positive", ChildArity::Exact(1), 1, false, false, JsonFast::InheritFirstChild),
    Integral => ("integral", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Regex => ("regex", ChildArity::Exact(1), 3, false, false, JsonFast::Never),
    StartsWith => ("starts_with", ChildArity::Exact(1), 2, false, false, JsonFast::Never),
    EndsWith => ("ends_with", ChildArity::Exact(1), 2, false, false, JsonFast::Never),
    Includes => ("includes", ChildArity::Exact(1), 2, false, false, JsonFast::Never),
    Ipv6 => ("ipv6", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Cidrv4 => ("cidrv4", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Cidrv6 => ("cidrv6", ChildArity::Exact(1), 1, false, false, JsonFast::Never),
    Trim => ("trim", ChildArity::Exact(1), 0, false, false, JsonFast::Never),
    Lower => ("lower", ChildArity::Exact(1), 0, false, false, JsonFast::Never),
    Upper => ("upper", ChildArity::Exact(1), 0, false, false, JsonFast::Never),
    Cleared => ("cleared", ChildArity::Any, 0, false, false, JsonFast::Never),
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn registry_is_in_enum_order_and_has_unique_names() {
        assert_eq!(KIND_SPECS.len(), Kind::Cleared as usize + 1);
        let mut names = HashSet::new();
        for (index, spec) in KIND_SPECS.iter().enumerate() {
            assert_eq!(spec.kind as usize, index);
            assert!(
                names.insert(spec.name),
                "duplicate kind name: {}",
                spec.name
            );
        }
    }
}
