"""Row-level security for every table that holds one person's private data.

Each policy calls one plpgsql function. The function returns true first for the
``system`` context, so the queries inside it (which run with the context set to
``system``) never re-enter a policy. See market/rls.py for how the context is set.
"""
from django.db import migrations

def _helper(name, args, declare, body):
    # Inner lookups switch to the system context for their own queries and switch back.
    # (A function-level SET clause would need a superuser for a custom setting.)
    return f"""
CREATE OR REPLACE FUNCTION {name}({args}) RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
DECLARE prev text := current_setting('kamiwa.mode', true); me bigint := kamiwa_uid(); result boolean; {declare}
BEGIN
  PERFORM set_config('kamiwa.mode', 'system', true);
  {body}
  PERFORM set_config('kamiwa.mode', coalesce(prev, ''), true);
  RETURN coalesce(result, false);
END $$;
"""


HELPERS = r"""
CREATE OR REPLACE FUNCTION kamiwa_is_system() RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT coalesce(current_setting('kamiwa.mode', true), '') = 'system' $$;

CREATE OR REPLACE FUNCTION kamiwa_uid() RETURNS bigint LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('kamiwa.uid', true), '')::bigint $$;

-- '*' marks the main administrator (superuser), who may read and change every row.
CREATE OR REPLACE FUNCTION kamiwa_has(perm text) RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT coalesce(current_setting('kamiwa.mode', true), '') = 'user'
          AND string_to_array(coalesce(current_setting('kamiwa.perms', true), ''), ',') && ARRAY[perm, '*'] $$;
""" + "".join([
    _helper("kamiwa_owns_listing", "lid bigint", "",
            "result := lid IS NOT NULL AND EXISTS (SELECT 1 FROM market_listing WHERE id = lid AND owner_id = me);"),
    _helper("kamiwa_owns_page", "pid bigint", "",
            "result := pid IS NOT NULL AND EXISTS (SELECT 1 FROM market_sellerpage WHERE id = pid AND user_id = me);"),
    _helper("kamiwa_is_my_expert", "eid bigint", "",
            "result := eid IS NOT NULL AND EXISTS (SELECT 1 FROM market_expertprofile WHERE id = eid AND user_id = me);"),
    # Requester, seller of the listing, or the assigned expert.
    _helper("kamiwa_inspection_party", "iid bigint", "", """result := me IS NOT NULL AND EXISTS (
    SELECT 1 FROM market_inspectionrequest r
    JOIN market_listing l ON l.id = r.listing_id
    LEFT JOIN market_expertprofile e ON e.id = r.expert_id
    WHERE r.id = iid AND (r.requester_id = me OR l.owner_id = me OR e.user_id = me));"""),
    # Staff see every account. Sellers, experts and staff appear by name on listings,
    # reports and ticket replies. A buyer is visible only to themselves and to the
    # seller and expert of that buyer's inspection request.
    _helper("kamiwa_can_account", "aid bigint, arole text", "staff boolean := kamiwa_has('staff');", """IF staff OR aid = me OR arole IN ('pro', 'plus', 'expert', 'staff') THEN
    result := me IS NOT NULL;
  ELSE
    result := me IS NOT NULL AND EXISTS (
      SELECT 1 FROM market_inspectionrequest r
      JOIN market_listing l ON l.id = r.listing_id
      LEFT JOIN market_expertprofile e ON e.id = r.expert_id
      WHERE r.requester_id = aid AND (l.owner_id = me OR e.user_id = me));
  END IF;"""),
    _helper("kamiwa_can_expert", "eid bigint, euser bigint", "", """result := me IS NOT NULL AND (euser = me OR EXISTS (
    SELECT 1 FROM market_inspectionrequest r JOIN market_listing l ON l.id = r.listing_id
    WHERE r.expert_id = eid AND (r.requester_id = me OR l.owner_id = me)));"""),
    _helper("kamiwa_owns_ticket", "tid bigint", "",
            "result := EXISTS (SELECT 1 FROM market_ticket WHERE id = tid AND owner_id = me);"),
    _helper("kamiwa_is_my_target", "t text", "",
            "result := t <> '' AND EXISTS (SELECT 1 FROM market_account WHERE id = me AND (lower(email) = lower(t) OR mobile = t));"),
])

# table -> SQL condition (columns of that table are in scope)
POLICIES = {
    "market_account": "kamiwa_can_account(id, role)",
    "market_payment": "user_id = kamiwa_uid() OR kamiwa_has('manage_billing')",
    "market_ticket": "owner_id = kamiwa_uid() OR kamiwa_has('support_tickets')",
    "market_ticketmessage": "kamiwa_has('support_tickets') OR kamiwa_owns_ticket(ticket_id)",
    "market_favorite": "user_id = kamiwa_uid() OR kamiwa_has('staff') OR kamiwa_owns_listing(listing_id)",
    "market_viewevent": "user_id = kamiwa_uid() OR kamiwa_has('staff') OR kamiwa_owns_listing(listing_id) OR kamiwa_owns_page(page_id)",
    "market_activityday": "user_id = kamiwa_uid() OR kamiwa_has('staff')",
    "market_otp": "user_id = kamiwa_uid() OR kamiwa_has('*')",
    "market_expertprofile": "kamiwa_has('finalize_report') OR kamiwa_has('manage_billing') OR kamiwa_can_expert(id, user_id)",
    "market_wallettransaction": "kamiwa_has('finalize_report') OR kamiwa_has('manage_billing') OR kamiwa_is_my_expert(expert_id)",
    "market_withdrawalrequest": "kamiwa_has('finalize_report') OR kamiwa_has('manage_billing') OR kamiwa_is_my_expert(expert_id)",
    "market_inspectionrequest": "kamiwa_has('finalize_report') OR requester_id = kamiwa_uid() OR kamiwa_owns_listing(listing_id) OR kamiwa_is_my_expert(expert_id)",
    # Final appraisal price and date are shown to every entitled buyer of the listing.
    "market_inspectionreport": "kamiwa_has('finalize_report') OR kamiwa_inspection_party(inspection_id) OR (status = 'final' AND kamiwa_uid() IS NOT NULL)",
    "market_auditevent": "actor_id = kamiwa_uid() OR kamiwa_has('staff')",
    "market_outboxmessage": "kamiwa_has('*') OR kamiwa_is_my_target(target)",
    "market_credentialrotation": "user_id = kamiwa_uid() OR kamiwa_has('*')",
}

# Writes are narrower than reads where a person may see a row they must not change.
WRITE_POLICIES = {
    # Staff lock and update other accounts while moderating; which staff may do what is checked in services.
    "market_account": "id = kamiwa_uid() OR kamiwa_has('staff')",
    "market_inspectionreport": "kamiwa_has('finalize_report') OR kamiwa_is_my_expert(expert_id)",
    "market_expertprofile": "kamiwa_has('finalize_report') OR kamiwa_has('manage_billing') OR user_id = kamiwa_uid()",
}

FUNCTIONS = ["kamiwa_is_my_target(text)", "kamiwa_owns_ticket(bigint)", "kamiwa_can_expert(bigint, bigint)",
             "kamiwa_can_account(bigint, text)", "kamiwa_inspection_party(bigint)", "kamiwa_is_my_expert(bigint)",
             "kamiwa_owns_page(bigint)", "kamiwa_owns_listing(bigint)", "kamiwa_has(text)", "kamiwa_uid()",
             "kamiwa_is_system()"]


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(HELPERS)
    for table, condition in POLICIES.items():
        write = WRITE_POLICIES.get(table, condition)
        schema_editor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE makes the policies apply to the table owner, which is the application role.
        schema_editor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        schema_editor.execute(f"DROP POLICY IF EXISTS kamiwa_read ON {table}")
        schema_editor.execute(f"DROP POLICY IF EXISTS kamiwa_write ON {table}")
        schema_editor.execute(f"DROP POLICY IF EXISTS kamiwa_update ON {table}")
        schema_editor.execute(f"DROP POLICY IF EXISTS kamiwa_delete ON {table}")
        schema_editor.execute(f"CREATE POLICY kamiwa_read ON {table} FOR SELECT USING (kamiwa_is_system() OR {condition})")
        schema_editor.execute(f"CREATE POLICY kamiwa_write ON {table} FOR INSERT WITH CHECK (kamiwa_is_system() OR {write})")
        schema_editor.execute(f"CREATE POLICY kamiwa_update ON {table} FOR UPDATE USING (kamiwa_is_system() OR {write}) WITH CHECK (kamiwa_is_system() OR {write})")
        schema_editor.execute(f"CREATE POLICY kamiwa_delete ON {table} FOR DELETE USING (kamiwa_is_system() OR {write})")


def backward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in POLICIES:
        for name in ("kamiwa_read", "kamiwa_write", "kamiwa_update", "kamiwa_delete"):
            schema_editor.execute(f"DROP POLICY IF EXISTS {name} ON {table}")
        schema_editor.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        schema_editor.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for signature in FUNCTIONS:
        schema_editor.execute(f"DROP FUNCTION IF EXISTS {signature}")


class Migration(migrations.Migration):
    dependencies = [("market", "0004_expert_wallet")]
    operations = [migrations.RunPython(forward, backward)]
