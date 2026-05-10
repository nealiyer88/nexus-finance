-- View: v_canonical_entities_with_refs
-- Aggregates system_references per canonical_id as a JSON object keyed by
-- source ('quickbooks', 'ruddr', ...). One row per canonical entity, even
-- if it has zero refs (refs is NULL in that case via LEFT JOIN).
--
-- Postgres only. Uses jsonb_object_agg + jsonb_build_object.

CREATE OR REPLACE VIEW v_canonical_entities_with_refs AS
SELECT
    ce.canonical_id,
    ce.tenant_id,
    ce.canonical_name,
    ce.entity_type,
    ce.entity_category,
    ce.confidence,
    ce.match_pattern,
    ce.match_signals,
    ce.created_at,
    ce.updated_at,
    refs.refs AS system_references
FROM canonical_entities ce
LEFT JOIN (
    SELECT
        sr.canonical_id,
        jsonb_object_agg(
            sr.source,
            jsonb_build_object(
                'category',        sr.category,
                'external_id',     sr.external_id,
                'external_fields', sr.external_fields
            )
        ) AS refs
    FROM system_references sr
    GROUP BY sr.canonical_id
) refs ON refs.canonical_id = ce.canonical_id;
