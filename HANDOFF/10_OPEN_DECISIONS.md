# Open Design Decisions

These decisions are intentionally not silently resolved in v0.4.0.

1. **Security audit log storage:** SQLite table, append-only JSON log, or both; retention and tamper evidence need design.
2. **Read-only repositories:** registry setting versus detected filesystem capability; service enforcement must be authoritative.
3. **Registry restore UX:** merge, replace, and preview semantics; rollback and path conflict rules.
4. **Inline review identity:** invite-scoped display identity versus future authenticated accounts.
5. **Conflict resolution:** three-way merge support without executing repository code.
6. **Git adapter:** subprocess Git versus a library; v0.4.1 should begin read-only status/diff only.
7. **External metadata mode:** identity and portability when `.forgetrace` cannot be written into the repository.
8. **Public networking:** no direct internet exposure until TLS, identities, permissions, audit, and abuse testing exist.
