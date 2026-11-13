# Initial setup

Start [local development](development.md#local-application) for evaluation or
follow [production installation](deployment.md). Create a staff account, sign in
and open **User → Administration**.

1. Add a property such as **Example House** with a slug such as `example-house`.
2. Add a vendor such as **Example Utilities**. Under **Vendor identification**, add
   its labelled organization/VAT identifier from the issuer part of an invoice.
   Keep actual identifiers private.
3. Select compatible **Invoice formats**. Upload a representative PDF and inspect
   its proposal if you need to identify its family. An unfamiliar structure uses
   manual review.
4. Add **Invoice property rules** for installation or contract keys. Each section of a
   shared invoice maps independently. Shared customer accounts and recipient
   postal addresses do not identify a property.
5. A vendor's optional **Default property** supplies a fallback. Specific rules
   and delivery-address mappings take precedence; conflicts require review.
6. Add **Property services** and dated **Service vendors**, then configure each
   vendor's shared **Billing schedule**. For waste costs appearing on only some
   bills, add **Service cost patterns** on the service vendor. Keep a general waste
   service for combined charges. Configure fraction services only for separately
   billed costs; invoice weights need no fraction service or cost pattern.
7. Enable Plast, Kartong and Glas under **Subcategories** when they are needed.
   Activation makes them available in review; unfamiliar PDF layouts still need
   manual review or parser support.

Use **Documents → Upload invoice** to preserve a PDF, then **Review and approve
invoice**. Compare reference, dates, totals, property/service allocations and
billed consumption against **View original PDF**. Displayed final days are
inclusive; stored periods are end-exclusive. Consumption has its own period and
basis and is not repeated for each price component.

Resolve warnings and missing fields before approving. Unsupported invoices can
be entered manually in the same form. Enter explicit cost allocation periods and
known consumption; leave unknown consumption absent. See [review](review.md).

Approval adds records to **Invoices** and **Reports**. Review electricity exclusions
and their source links before relying on totals. Repeated uploads open the existing
document. Corrections retain earlier approvals; re-parsing proposes changes for
explicit acceptance.

Configuration is stored in SQLite and survives image replacement. Installation
paths and secrets belong in private environment configuration. Verify
[backup and clean restoration](backup-and-recovery.md) before normal real-data use.

Migration `0025_waste_fractions` preserves approved amounts, weights and review JSON,
groups garden with waste collection, and leaves cost patterns unset. Establish real
cost frequencies and billing seasons from the vendor arrangement. Reversing it
removes group/fraction-kind/pattern metadata, while retaining catalogue entries.
Reversing further to a pre-catalogue schema can be blocked by those extra entries;
use a pre-upgrade backup when restoring that older installation.
