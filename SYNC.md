# Server-side layout (fork ConstantinGuyader/hermes-pinned-folders)

This fork stores the folder layout on the connected Hermes backend instead of
only in the Desktop app:

- `dashboard/plugin_api.py` serves `GET/PUT /api/plugins/pinned-folders/layout`
  and keeps the layout in `$HERMES_HOME/pinned-folders/layout.json`.
- The Desktop half polls it every 4 s and pushes changes (debounced, with a
  revision check; on conflict the server wins).
- Every Desktop app on the same backend (PC, Mac) shows the same folders, and
  an agent on the server can file chats by editing `layout.json` directly.
- Collapse state and the view menu stay local to each app.
- Without the backend half (plugin not enabled on the server) the plugin works
  exactly like upstream: local-only.

Layout file shape: `{ folders: [{id, name, parent, color?}], placed: {sessionId: folderId}, order: [sessionId], foldersOrdered: true }`.
Only pinned chats are shown, so pin a chat (`hermes sessions pin <id>`) before placing it.
