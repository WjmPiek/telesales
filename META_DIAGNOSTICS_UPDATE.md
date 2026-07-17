# Meta diagnostics update

The WhatsApp Settings connection test now performs four independent checks:

1. Access token (`/{version}/me`)
2. Phone Number ID
3. WhatsApp Business Account ID
4. Template-list permission

For every check, the page displays the HTTP status, endpoint, parsed Meta response, Meta error code/subcode, trace ID, and a clear pass/fail state. The access token is never displayed or stored in the report.

Deploy the project, open **WhatsApp > Settings**, and click **Run full Meta diagnostics**.
