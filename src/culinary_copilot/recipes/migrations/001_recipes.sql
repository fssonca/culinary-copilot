CREATE TABLE recipe_imports (
    id text PRIMARY KEY,
    dataset_id text NOT NULL,
    revision text NOT NULL,
    checksum text NOT NULL,
    normalizer_version text NOT NULL,
    vocabulary_checksum text NOT NULL,
    dataset_url text NOT NULL,
    report jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE recipes (
    dataset_id text NOT NULL,
    source_id text NOT NULL,
    import_id text NOT NULL REFERENCES recipe_imports(id),
    title text NOT NULL,
    total_minutes double precision,
    servings double precision,
    ingredient_names text[] NOT NULL,
    document jsonb NOT NULL,
    search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS
        (to_tsvector('english'::regconfig, search_text)) STORED,
    PRIMARY KEY (dataset_id, source_id)
);
CREATE INDEX recipes_fts ON recipes USING gin(search_vector);
CREATE INDEX recipes_ingredients ON recipes USING gin(ingredient_names);
CREATE INDEX recipes_time ON recipes(total_minutes);
CREATE TABLE recipe_quarantine (
    import_id text NOT NULL REFERENCES recipe_imports(id),
    row_number integer NOT NULL,
    reason text NOT NULL,
    raw jsonb NOT NULL,
    PRIMARY KEY (import_id, row_number)
);
