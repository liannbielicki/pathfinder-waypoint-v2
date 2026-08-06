# Production inputs that must not be guessed

These do not block repository bootstrap, durable orchestration, UI construction, or fixture-driven implementation. They must be resolved before their named production gate can pass.

1. **Audience identity contract:** provide an approved clean-audience fixture containing `pro_id` and `org_uuid`, or approve a resolver contract. Suppression remains upstream and must not be rebuilt here.
2. **Persona fit contract:** approve the permitted match features, their weighting, and the minimum fit threshold used by both closest personas and related counterweights. The panel sizes and 2+1/3+2 composition are already closed.
3. **LCM V2 contract:** obtain Allison's recorded request/response fixture for measurement-plan and audience-lineage fields. Until then, build the durable internal artifact and use a fake/staging adapter only.
4. **Production runtime values:** provide Railway, Supabase, Vercel, n8n, model, persona, and LCM staging access; choose the launch model IDs and dollar ceilings.
5. **Capacity environment:** activate `org-context-v2` and provide an approved 200-member test audience plus production-equivalent rate limits and a non-sending LCM endpoint for the real launch load gate.

An implementing agent must report the exact missing item and continue with independent tasks. It must not invent a contract or silently weaken a launch gate.
