# VK notification fixtures

Derived from actual Gmail text/html and text/plain alternatives inspected on
2026-10-10, preserving table nesting, classes, styles and tracking-link structure.
Names, message previews, profile paths, peer/message IDs, recipients, avatars and
all tracking/authentication/unsubscribe parameters were replaced or removed.
These are sanitized structural fixtures, not original emails or DKIM evidence.

- single_1302 / single_1503: linked timestamp in a nested two-row sender table;
  actual message in the next row of its enclosing card, separate Reply button.
- digest: three independent cards; first has only a generic “сообщение” link,
  others have previews; timestamp follows each preview in its own row.
- community: negative peer ID, single card with additional media rows.
- Plain single notifications omit the message timestamp. Plain digest exposes
  only a count and unread-inbox link, not individual messages.

Authentication tests synthesize headers; they do not perform DKIM verification.
