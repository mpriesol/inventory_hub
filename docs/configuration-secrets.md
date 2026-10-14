# Integration configuration and credentials

Implemented in the security boundary release. Shop, supplier and console
configuration endpoints require authenticated Hub access. Creating/removing a
supplier, changing or restoring integration configuration, and validating its
remote URLs additionally require an administrator account. The existing server
operator bearer token remains an administrator-compatible recovery credential.
Normal refreshes of configured feeds remain available to operators; overriding
their URL is administrative.
Saving or replacing the separate Sloger B2B login also requires an administrator;
operators can check whether it is configured and use it for normal product work.

Configuration GET, save, history and restore responses contain no stored API
keys, passwords, cookies or authentication tokens. Supplier feed/export URLs can
act as bearer credentials through their path, so those URLs are also write-only.
Arbitrary remote headers, query parameters, POST bodies and authentication
`value` fields receive the same protection. Server-side loaders still read the
original values for their normal integrations.

The response leaves an empty value at the original location and adds a boolean
sibling, for example `password: ""` and `password_configured: true`. Hidden lists
and dictionaries are represented by `[]` and `{}` with the same status sibling.
The supplier UI shows that the value is saved without downloading it.

- Omitting a credential or submitting its empty value preserves the saved value.
- A nonempty replacement updates it. Mask strings are rejected rather than saved
  as credentials.
- Configured-status metadata is response-only and is never persisted.
- Replacing a single feed URL with a URL list, or the opposite, clears the previous
  hidden alternative so it cannot override the newly entered endpoint.
- Removing a complete feed source still removes that source.
- Explicit credential removal uses `_clear_secrets`, an array of JSON pointers,
  such as `{"_clear_secrets": ["/invoices/download/web/login/password"]}`. This
  preserves the rest of the saved configuration and consumes the instruction.
- Restoring a history version uses the original server-side values, not the
  redacted browser representation.

Newly written configuration and history files use mode `0600`. This does not
encrypt credentials at rest or retrospectively change permissions of untouched
files. Protect the persistent volume and its backups through server ownership and
backup access controls. Raw configuration/history files are not downloadable.

Supplier/export request failures no longer echo exception strings containing
credential-bearing URLs. Legacy feed refresh and reachability checks verify TLS
certificates. Existing configured credentials are preserved at deployment; rotate
previously exposed integration credentials separately after closing the boundary.

Validation: `api/tests/test_config_secrets.py` exercises secret-free responses,
ordinary save round trips, replacement/removal/history restoration, malformed
identifiers, safe errors and administrator HTTP dependencies on temporary data.
These tests do not contact suppliers or mutate production configuration.
