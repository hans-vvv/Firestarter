# Adding a New Service

This document describes the steps required to introduce a new service into the system.

The architecture is intentionally pluggable: services are defined declaratively and implemented through isolated components that integrate into the orchestration pipeline.

A new service consists of:

1. YAML specification
2. Feature Handler implementation
3. Orchestrator registration
4. Context layer integration
5. Jinja2 templates

Each step is described below with a focus on files and responsibilities.

---

## 1. Define YAML Specifications

Path: environment data under the data root at `services/definitions/` (one
`*_def.yaml` per service / tenant / variant; see `app/domain/file_locations.py`).

The demo ships two service families as worked examples: the `vprn` service (an L3
VPN exposed to the access switch on VLAN 20, carrying `subnet_info.ce_mgmt: true`
so every CE gets a management address in it) and the `evpn_vpls` service (an
EVPN-VPLS exposed on VLAN 30). Their handlers in
`app/services/service_handling/feature_handlers/` document every parameter.

### Responsibilities
- Represent intent only, not computed state
- Basic validation is provided and enforced in Service Orchestrator (see `app.validations`)
- Ensure structure is well-defined and consistent

### Constraints
- The following keys are mandatory and must be unique in combination:
  - `service`
  - `variant`
  - `tenant`
- Device selectors are required to determine the target device scope

Examples of supported selectors can be found in the test suite.

### Notes
- YAML must remain vendor-agnostic
- Avoid embedding topology-derived values
- Prefer explicit fields over implicit defaults
- For stricter validation, implement Pydantic schemas where needed
- Avoid relying on implicit dictionary access unless you know what you are doing (i.e.: use Pydantic schema's or accept the risk of a crashing app)

---

## 2. Implement Feature Handler

Path: `app.services.service_handling.feature_handlers`  
[View feature handlers](../app/services/service_handling/feature_handlers/)

### Responsibilities
- Translate service intent + topology → computed intent
- Produce deterministic results
- Remain stateless (except reading persisted topology and allocations) 

### Outputs
- Device-scoped intent data. See for example the EVPN L2VPN feature handler how you should structure the returned data using Tree object.
- Service/protocol-specific structures

### Guidelines
- This is the core logic layer of the service
- Keep all domain-specific computation here
- Do not include rendering or vendor-specific logic

---

## 3. Register in Service Orchestrator

Module: `app.services.service_handling.service_orchestrator`  
[View source](../app/services/service_handling/service_orchestrator.py)

### Responsibilities
- Integrate the service into the orchestration pipeline
- Ensure correct execution order

### Required Changes
- Add a `ServiceDescriptor` for the new service to `SERVICE_DESCRIPTORS`; its
  position in that list is its execution order (today: isis, sr, bgp, evpn_esi,
  evpn_vpls, vprn)
- Add the service to the catalogue categories in `app/web/service_catalogue.py`
  if it should appear on the Services page (`infra` or `services`)

---

## 4. Extend Context Layer

Path: `app.services.context`  
[View context directory](../app/services/context/)

### Responsibilities
- Merge computed intent into the device render context
- Normalize data structures for template consumption

### Typical Changes
- Map service intent into:
  - device-level context
  - interface-level context (if applicable)

### Guidelines
- Keep transformations minimal and deterministic
- Do not introduce business logic

---

## 5. Create Jinja2 Templates

Path: `app.services.templates.sros`  
[View template directory](../app/services/templates/sros/)

### Responsibilities
- Render final configuration from context data

### Guidelines
- Only use simple conditionals and loops
- Do not perform topology or protocol computations
- Keep templates vendor-specific but structurally consistent

---

## Execution Flow

1. YAML is defined with all required parameters
2. Service Orchestrator schedules execution
3. Feature Handler computes intent
4. Results are persisted per ServiceInstance
5. Context Layer builds device-level context
6. Templates render final configuration

---

## Printer

Module: `app.printing`  
[View source](../app/printing/printer.py)

Used to render and output device configurations.

---

## Design Constraints

When adding a new service, ensure:

- Deterministic output (same input → same configuration)
- Clear separation of concerns:
  - YAML = intent
  - Handler = computation
  - Context = composition
  - Template = rendering
- No vendor-specific logic outside templates
- No topology mutation during service computation

---

## Minimal Checklist

- [ ] YAML definition created
- [ ] Example YAML added
- [ ] Feature Handler implemented
- [ ] Handler registered in orchestrator
- [ ] Context layer updated
- [ ] Templates created (at least one vendor)
- [ ] Unit tests implemented and passing