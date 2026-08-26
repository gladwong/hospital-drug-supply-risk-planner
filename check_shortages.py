import duckdb

con = duckdb.connect("db/supply_risk.duckdb")
result = con.execute(
    "SELECT generic_name, strength FROM shortages WHERE strength IS NOT NULL LIMIT 5"
)
print(result.fetchdf().to_string())
con.close()