## Summary

Describe the user-visible result and the component boundary affected.

## Verification

- [ ] `powershell -ExecutionPolicy Bypass -File .\tools\verify.ps1`
- [ ] The affected native or provider flow was checked when source tests cannot prove it.
- [ ] New behavior has a focused regression test, or the reason it cannot is documented.

## Privacy, security, and compatibility

- [ ] No prompt, transcript, tool body, credential, environment variable, full workspace path, or
      local runtime artifact was added.
- [ ] Protocol, persistence, or cross-component behavior is reflected in the owning contract.
- [ ] Failure behavior is bounded and preserves provider approval boundaries.
- [ ] Rollback or compatibility impact is described when relevant.
