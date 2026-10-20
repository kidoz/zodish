# Zodish documentation

Zodish documentation follows the [Diátaxis](https://diataxis.fr/) framework. Every
page belongs to one of four categories and serves one purpose, so you can go
straight to the kind of help you need.

| Category | Purpose | Start here |
| --- | --- | --- |
| [Tutorials](tutorials/) | Learn Zodish by doing, in a guided sequence | [Get started](tutorials/get-started.md) |
| [How-to guides](how-to/) | Solve a specific task you already understand | [Validate structured data](how-to/validate-data.md) |
| [Reference](reference/) | Look up exact APIs, codes, and export rules | [API reference](reference/api.md) |
| [Explanation](explanation/) | Understand the design and the reasoning | [Architecture](explanation/architecture.md) |

## I want to…

| Task | Read |
| --- | --- |
| Build and validate my first schema | [Tutorial: Get started](tutorials/get-started.md) |
| Validate objects, arrays, tuples, records, or unions | [Validate structured data](how-to/validate-data.md) |
| Return errors to an API client or a form | [Handle errors](how-to/handle-errors.md) |
| Distinguish missing input, `None`, and defaults | [Handle missing values](how-to/handle-missing-values.md) |
| Refine, transform, or preprocess values | [Transform and refine values](how-to/transform-and-refine.md) |
| Convert between encoded and decoded values | [Use codecs](how-to/use-codecs.md) |
| Understand whether validation can be async | [Async support](explanation/async-support.md) |
| Produce JSON Schema or OpenAPI schemas | [Export JSON Schema](how-to/export-json-schema.md) |
| Contribute, test, or benchmark | [Contribute to Zodish](how-to/contribute.md) |
| Build and verify wheel/sdist artifacts | [Build distributions](how-to/build-distributions.md) |
| Look up a constructor or method | [API reference](reference/api.md) |
| Look up an issue code or error field | [Errors reference](reference/errors.md) |
| Check what JSON Schema export supports | [JSON Schema reference](reference/json-schema.md) |

## How the documentation is organized

- **Tutorials** are lessons. They take you from a starting point to a working
  result and explain only what you need at each step.
- **How-to guides** are recipes. They assume you know the basics and show how to
  complete one real task.
- **Reference** is a lookup. It describes behavior precisely and does not teach.
- **Explanation** is discussion. It covers design decisions, trade-offs, and
  mental models.

If a page mixes categories, the content should be split rather than grown. Keep
each page focused on the question its category answers.

## Conventions

- Examples target CPython 3.14+ and assume `pip install zodish`.
- `z` is the schema builder namespace: `from zodish import z`.
- The project uses [Semantic Versioning](https://semver.org/); see the
  [changelog](../CHANGELOG.md) for released changes.
