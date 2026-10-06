-- Domain model. Names follow the blueprint's schema; additions are marked (+).

CREATE TABLE terminals (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants UNIQUE, name text NOT NULL, model_seed int NOT NULL, day date NOT NULL, now_min int NOT NULL);   -- (+)
SELECT enable_tenant_rls('terminals');

CREATE TABLE vessels (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, terminal_id uuid NOT NULL REFERENCES terminals ON DELETE CASCADE, ordinal int NOT NULL, imo text NOT NULL, name text NOT NULL, loa_m numeric NOT NULL, draft_m numeric NOT NULL,
  UNIQUE (terminal_id, imo));
SELECT enable_tenant_rls('vessels');

CREATE TABLE port_calls (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, vessel_id uuid NOT NULL UNIQUE REFERENCES vessels ON DELETE CASCADE, eta timestamptz NOT NULL, etd timestamptz NOT NULL, actual_arrival timestamptz, status text NOT NULL, service text NOT NULL,
  predicted_eta timestamptz, eta_min int NOT NULL, predicted_min int, discharge int NOT NULL, load int NOT NULL, priority int NOT NULL);   -- (+) prediction, minutes, moves, priority
SELECT enable_tenant_rls('port_calls');

CREATE TABLE ais_positions (tenant_id uuid NOT NULL REFERENCES tenants, vessel_id uuid NOT NULL REFERENCES vessels ON DELETE CASCADE, t_min int NOT NULL, distance_nm numeric NOT NULL, speed_kn numeric NOT NULL, PRIMARY KEY (vessel_id, t_min));   -- (+)
SELECT enable_tenant_rls('ais_positions');

CREATE TABLE berths (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, terminal_id uuid NOT NULL REFERENCES terminals ON DELETE CASCADE, ordinal int NOT NULL, code text NOT NULL, length_m numeric NOT NULL, depth_m numeric NOT NULL, cranes_available int NOT NULL);
SELECT enable_tenant_rls('berths');

CREATE TABLE berth_allocations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, port_call_id uuid NOT NULL UNIQUE REFERENCES port_calls ON DELETE CASCADE, berth_id uuid NOT NULL REFERENCES berths, start_ts timestamptz NOT NULL, end_ts timestamptz NOT NULL,
  locked boolean NOT NULL DEFAULT false, cranes int NOT NULL, start_min int NOT NULL, end_min int NOT NULL, source text NOT NULL);   -- (+) cranes, minutes, which planner
SELECT enable_tenant_rls('berth_allocations');

CREATE TABLE containers (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, terminal_id uuid NOT NULL REFERENCES terminals ON DELETE CASCADE, ordinal int NOT NULL, iso_no text NOT NULL, size_type text NOT NULL, weight_kg numeric NOT NULL, hazard_code text,
  destination_mode text NOT NULL, customs_status text NOT NULL DEFAULT 'cleared', vessel_id uuid NOT NULL REFERENCES vessels, reefer boolean NOT NULL, pickup_after_h real NOT NULL, UNIQUE (terminal_id, iso_no));   -- (+) vessel, reefer, pickup
SELECT enable_tenant_rls('containers');

CREATE TABLE yard_slots (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, terminal_id uuid NOT NULL REFERENCES terminals ON DELETE CASCADE, block text NOT NULL, bay int NOT NULL, row_no int NOT NULL, tier int NOT NULL, reefer boolean NOT NULL DEFAULT false,
  hazard_allowed boolean NOT NULL DEFAULT true, container_id uuid REFERENCES containers, UNIQUE (terminal_id, block, bay, row_no, tier));
SELECT enable_tenant_rls('yard_slots');

CREATE TABLE container_moves (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, container_id uuid NOT NULL REFERENCES containers ON DELETE CASCADE, move_type text NOT NULL, planned_ts timestamptz NOT NULL, actual_ts timestamptz, from_loc text NOT NULL, to_loc text NOT NULL,
  equipment_id uuid);
SELECT enable_tenant_rls('container_moves');

CREATE TABLE gate_hours (tenant_id uuid NOT NULL REFERENCES tenants, terminal_id uuid NOT NULL REFERENCES terminals ON DELETE CASCADE, hour int NOT NULL, trucks_expected int NOT NULL, appointments int, PRIMARY KEY (terminal_id, hour));   -- (+)
SELECT enable_tenant_rls('gate_hours');

CREATE TABLE plans (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, terminal_id uuid NOT NULL REFERENCES terminals ON DELETE CASCADE, kind text NOT NULL, summary jsonb NOT NULL, created_by uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now());   -- (+)
SELECT enable_tenant_rls('plans');
