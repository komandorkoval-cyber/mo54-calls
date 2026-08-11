# Certificates for GigaChat TLS

GigaChat presents a certificate chain rooted at the Russian Trusted Root CA.
The worker installs the official root and issuing certificates into Debian's
system trust store when its image is built. TLS verification remains enabled.

Sources (official):

- https://developers.sber.ru/docs/ru/gigachat/certificates
- https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
- https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt

When the certificates are replaced, download both files again from the official
URLs, inspect their subject and expiry with `openssl x509`, rebuild the worker,
and run the TLS health check. Do not replace this mechanism with `verify=False`.
