# Raw SQL over an ORM

Data access uses psycopg3 with hand-written SQL, and the schema is a numbered `.sql` file
applied at database initialisation rather than an Alembic migration history.

The schema's interesting parts — a generated `geography` column, a GiST index, `ON CONFLICT`
idempotency, PostGIS predicates — are all things a migration tool cannot autogenerate and
would have to be hand-written regardless. Choosing an ORM would have added the framework's
ceremony without its main benefit, and buried the index and query design that this project
most needs to make legible.

## Consequences

Dynamic filter composition is hand-rolled, which is where SQL injection would enter if done
carelessly. Filters must therefore return `(fragment, params)` pairs that are joined and
passed to the driver as parameters; values must never be interpolated into a query string.

There is no migration history. The schema is applied once to a fresh database. A deployment
that must evolve a populated schema needs Alembic or Flyway introduced first.
