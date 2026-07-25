# API and Schema Notes

## Version endpoints

- `GET /api/v1/version`
- `GET /api/version` compatibility alias
- `HEAD` is supported where GET metadata/static checks are valid

## Long operations

Persistent job routes expose operation status and cancellation. New import flows should use the job service rather than holding a browser request open for the complete copy.

## Imports

The owner API now separates:

1. preview/enumeration;
2. conflict/sensitive/capacity decision;
3. persistent job creation;
4. progress/cancel;
5. transactional commit and verification.

Do not reintroduce direct `copytree`-style mutation into live repositories.

## Sensitive exports and sharing

Sensitive paths are returned in preview metadata. Export/share must require explicit inclusion. Never place raw invitation tokens in persistent registry/repository state.

## Repository state

Repository schema 2 includes revisioned atomic state, richer snapshot entries, and identity metadata. `.forgetrace/file-index.json` is a cache, not a source of truth. Transaction journals are recoverable implementation state and must remain protected from normal file APIs.
