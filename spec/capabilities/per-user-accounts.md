# Capability: Per-User Accounts
**Phase 5**

## What It Does
Replaces the shared password with individual logins, so every decision, stage change and send is attributed to a person.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Users (name, email, password, role admin/member) | form | Admin | Yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| `actor_user_id` filled on all new records | data | Timeline ("Accepted by Priya") |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none | — |

## Business Rules
- Passwords are stored only as a slow salted hash. Admins manage users. Historical records with a null actor show "Team".

## Success Criteria
- [ ] A user's accept writes `actor_user_id`, and the timeline shows their name.
- [ ] A member cannot open user management (403).
