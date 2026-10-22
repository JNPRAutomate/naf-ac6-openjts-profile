# New Profile

Create a tgz file with the content of a folder with the name of your profile (e.g. naf)

```bash
ls -lart naf/
total 52
-rw-rw-r-- 1 hpen hpen  7961 Oct 14 13:07 crpd_naf.original.json
-rw-rw-r-- 1 hpen hpen 10935 Oct 14 13:07 crpd_naf.json
-rw-rw-r-- 1 hpen hpen   372 Oct 19 14:18 definition.json
-rw-rw-r-- 1 hpen hpen  1369 Oct 20 10:40 csrx_naf.json
drwxrwxr-x 2 hpen hpen  4096 Oct 21 09:52 .
-rw-rw-r-- 1 hpen hpen 11198 Oct 22 08:21 dashboard-naf.json
drwxr-xr-x 3 hpen hpen  4096 Oct 22 09:27 ..
```

containing a few files:

- A dashboard (dashboard-naf.json)
- The definition (definition.json)
- The family devices which can be associated to that profile (e.g. crpd_naf.json and csrx_naf.json) containing the XPath (NETCONF) or PATH (gNMI) for each sensor
