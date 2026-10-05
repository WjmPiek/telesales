# Individual clients: name and cell number only

1. On **Insurance Sales Lead Queue**, choose **Download individual clients template**.
2. Fill in **Name** and **Cell_Number**, one contact per row. Keep cell numbers as text to preserve leading zeroes. Local SA numbers, +27 numbers and Excel numeric cells are accepted.
3. Use **Import individual clients**. Choose an existing company, or leave **Individual Clients group** selected to create/use that group under your branch.
4. Create the WhatsApp campaign for the same company/group. On its recipient page, choose **Individual clients only**, click **Filter**, then **Add all matching clients** or select individual contacts.
5. Send the existing approved image template with **APPLY NOW**. The client's personal link opens the existing ID/product qualification, application questionnaire, document signing and supporting-document flow.
6. When the client saves the questionnaire, their application and contact details are saved, their lead advances to **Application Started**, and the assigned consultant receives a notification. The existing signing, FICA, review and activation requirements continue to apply. A client who declines marketing stays opted out.

Importing does not send messages or create policy/application records. No policy number, ID number, email, premium, product or cover is required for the import. These prospects are distinct from existing policy imports and are exempt from the policy ID suspense check while they have a contact number.

Invalid rows reject the entire import with row numbers. Duplicate normalized phone numbers within the chosen company/group are skipped without overwriting existing contact/policy details. Maximum: 10,000 rows and a 5 MB XLSX file.

## Render / PostgreSQL

Deploy this change through the usual GitHub/Render process. Startup adds `lapsed_policies.lead_type` with a default of `policy`, plus an index, using idempotent PostgreSQL statements. Existing leads remain policy leads; existing rows are not deleted or rewritten. A manually-created individual campaign contact is marked `prospect` from this version onwards. The database user needs permission to alter this table, as required by the app's existing startup upgrades.

Use the existing production `BASE_URL` and WhatsApp provider configuration. The approved WhatsApp template must include its existing dynamic **APPLY NOW** URL (`/join/{{1}}`); an older approved template without that button must be replaced/approved through the existing template workflow.

## Validation

`python -m unittest discover -s tests -p test_prospect_import.py -v`

The tests cover template download, name/phone-only import, phone normalization, duplicate handling, atomic validation, import permissions, suspense checks, company/recipient filtering, WhatsApp invitation parameters, public application submission, consultant notifications and preserved marketing opt-outs.
