# PUBLISH — checklist de publicación marketplace

Estado base (verificado 2026-09-09): `omarchy plugin validate .` limpio,
`usage.py doctor` con 6 providers detectados, `preview.png` real en la raíz.

## Convenciones (vs `omarchy-news-feed`, ya publicado)

- README con Highlights / Install (`omarchy plugin add <url> --enable`) /
  Configure. El nuestro es más corto: ampliar antes de enviar (dueño: README,
  otro carril).
- `manifest.json`: id `synapsync.usage-notch`, `license: MIT`, `homepage`
  pendiente (añadir URL del repo al crearlo).
- `LICENSE` MIT a nombre de Roimer Peraza: OK. `NOTICE.md` con atribución a
  Codenotch: OK (los reviewers la van a leer; mantenerla).
- Lección del news-feed: el review marketplace es estricto en seguridad
  (SSRF, topes de cuerpo, redirect scope). Nuestro backend ya sigue esa
  disciplina (endpoints fijos https, IPs globales + SNI, deadline 15 s,
  tope 256 KB, redirects mismo-host). No relajarla.

## Pasos

1. `git commit` inicial en este repo + `git remote add origin
   git@github.com:joisephdev/omarchy-usage-notch.git` + push.
2. Añadir `homepage` al manifest apuntando al repo (edita manifest otro
   carril o el padre; revalidar después).
3. Probar instalación por URL en limpio:
   `omarchy plugin add https://github.com/joisephdev/omarchy-usage-notch --enable`
   (si ya existe el symlink/copia local, probar con `--yes` o en otra cuenta).
4. Abrir el issue de submission en `omarchy-plugin-marketplace` con: id,
   repo, descripción, `preview.png`, kind `panel`, nota de que requiere
   sesiones ya iniciadas en las CLIs (no hace login propio).
5. Responder reviews iterando (el news-feed pasó 3 rondas: 197e1b1,
   999f71f, 4a0f780 como referencia de tono/formato de fixes).

## REGLA CRÍTICA — jamás romper

**NUNCA** `Fixes #N` / `Closes #N` / `Resolves #N` en mensajes de commit,
ni contra el issue de submission ni contra ningún issue del marketplace:
GitHub auto-cierra el issue al mergear y arruina el proceso de review.
Usar siempre `Refs #N` (referencia sin cierre). Vale para PRs también.
