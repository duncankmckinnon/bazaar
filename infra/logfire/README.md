# Logfire dashboard

`bazaar-strategy.json` is the exported definition of the custom `Bazaar strategy` dashboard
in the `bazaar-demo` Logfire project. The definition is managed with the official
`pydantic/logfire` Terraform provider.

The provider reads `LOGFIRE_API_KEY`. Its key must reach `bazaar-demo` and have
`project:read_dashboard` and `project:write_dashboard` scopes.

Copy `terraform.tfvars.example` to `terraform.tfvars` and replace the placeholder with the
project UUID shown in the Logfire project settings. Adopt the existing dashboard once before
the first apply:

```console
terraform init
terraform import logfire_dashboard.bazaar_strategy "bazaar-demo/bazaar-strategy"
terraform plan
terraform apply
```

After adoption, edit or replace `bazaar-strategy.json`, review `terraform plan`, and apply it.
The resource has deletion protection so an ordinary apply cannot delete the live dashboard.
