-- The topics a message about several was split into, each searched on its own (query_rewrite.v2). Additive only.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sub_queries text[];
