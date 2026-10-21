# NCR fictifs — DS-MVTec / cable

## Contenu

- `ncr_cable_synthetiques.jsonl` : 1 objet JSON par image, pratique pour la préparation de la base vectorielle.
- `ncr_cable_synthetiques.csv` : même contenu sous forme tabulaire, pour revue et filtrage.
- `ncr_cable_synthetiques_summary.json` : inventaire et décompte par classe.

## Couverture

150 images répertoriées dans `DS-MVTec/cable/image` : 92 enregistrements NCR fictifs pour les classes défectueuses et 58 fiches de référence `good` sans NCR. Les fiches `good` sont utiles comme exemples négatifs, mais ce ne sont pas des NCR au sens strict.

## Utilisation RAG

Indexer de préférence `embedding_text` ou `ncr_text` et conserver `record_id`, `image_path`, `defect_class`, `is_ncr` et `synthetic` comme métadonnées filtrables. Cela facilite le filtrage des exemples fictifs et des références négatives.

## Structure qualité adoptée

Le format s'inspire des pratiques courantes des formulaires NCR : identification et traçabilité, exigence applicable, constat et preuve, impact préliminaire, confinement, disposition, analyse de cause, actions correctives, vérification d'efficacité et statut de clôture.

Le référentiel général est ISO 9001:2026, notamment §8.7 (contrôle des éléments de sortie non conformes) et §10.2 (non-conformité et action corrective). Le standard définit les exigences du système de management ; il n'impose pas un unique formulaire NCR prêt à l'emploi. Références de consultation :

- ISO 9001:2026 (page officielle ISO) : https://www.iso.org/standard/9001
- Résumé public de la clause 8.7 : https://sterling-grc.com/standards/iso-9001/requirements/clause-8-7/
- Exemple de structure d'un formulaire NCR : https://millwrightqc.com/templates/ncr.html
- Dataset : https://huggingface.co/datasets/DefectSpectrum/Defect_Spectrum/tree/main/DS-MVTec/cable/image

## Limites à garder en tête

- Ces enregistrements sont fictifs et destinés à un projet d'étude.
- Les rapports défectueux sont générés à partir du nom de la classe et du chemin d'image ; les pixels de chaque image et le fichier `captions.xlsx` n'ont pas été individuellement analysés dans cette génération.
- Les champs de traçabilité absents du dataset (référence de pièce, révision, lot, quantités, mesures, dates, responsables) restent nuls ou explicitement non renseignés.
- Les exigences techniques sont des hypothèses de scénario à confirmer par le plan ou la spécification applicable.
- Les causes racines ne sont jamais déclarées confirmées ; les dispositions, actions et vérifications sont proposées/non exécutées.
- Une classe `good` ne produit pas de NCR artificiel : elle produit une fiche de référence négative et ne prouve pas la conformité complète du produit.

Ces données sont des exemples d'entraînement et ne doivent pas être utilisées comme des enregistrements qualité opérationnels.
