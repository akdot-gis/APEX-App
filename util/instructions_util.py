import streamlit as st


TAB_INSTRUCTIONS = {
    "Load Geometry": """
##### Step 1: Select Project Type
Start by selecting how the project footprint will be represented on the map.  
The available project types depend on the **source of the project data**.

- **Site Projects** *(AASHTOWare or User Input)*  
  Represent projects tied to a specific location and displayed as a single point.  
  Examples include airports, harbors, maintenance facilities, or localized work areas not spanning roadway segments.

- **Route Projects** *(AASHTOWare or User Input)*  
  Represent projects occurring along the Alaska DOT&PF roadway network and displayed as a line.  
  Examples include construction, resurfacing, safety improvements, or maintenance work affecting one or more roadway segments.

- **Boundary Projects** *(User Input only)*  
  Represent projects defined by one or more polygonal areas rather than points or routes.  
  Boundary projects may consist of a single area or multiple areas combined together to form the project footprint.  
  This option is **only available** when the project source is **User Input** and is not supported for AASHTOWare‑sourced projects.

Your selection determines which source options and geometry tools will be available in the next step.

---
##### Step 2: Load Project Footprint
After selecting a project type, choose how the footprint will be created.  
The available options depend on whether the project is **AASHTOWare‑sourced** or **User Input**.

**AASHTOWare‑Sourced Projects**

If your project is sourced from AASHTOWare and sufficient geometry exists, an **AASHTOWare** option will appear automatically.

- **Site Projects (AASHTOWare)**  
  The application looks for **Midpoint** records stored in AASHTOWare.  
  If sufficient midpoint data exists, the AASHTOWare tab will appear and allow you to generate a site footprint using those stored midpoints.

- **Route Projects (AASHTOWare)**  
  The application looks for matching **Begin (BOP)** and **End (EOP)** points that share the same **Route ID**.  
  When sufficient data exists, the footprint is created by clipping the Alaska Route System between the begin and end points.  
  Multiple route segments may be combined when multiple valid begin and end point pairs are present.

If an AASHTOWare option does **not** appear, it means sufficient geometry is not available.  
In this case, you may use one of the manual options below.

**Manual Options (Available to All Projects When Needed)**

- **Site Projects:**  
  - Upload a shapefile  
  - Enter latitude and longitude coordinates  
  - Select a point directly on the map

- **Route Projects:**  
  - Upload a shapefile  
  - Draw a route directly on the map

**User Input Projects Only**

- **Boundary Projects:**  
  Boundary footprints must be created manually.  
  - Upload a polygon shapefile  
  - Draw one or more boundaries directly on the map

Once a footprint is created, it will be displayed on the map for review before proceeding.

---

##### Step 3: Confirm Accuracy
After uploading, carefully review the geometry displayed on the map.  
Make sure the location or route matches your project details.  
Confirming accuracy at this stage is important because the geometry will be used in later steps for analysis and reporting.  
If something looks incorrect, adjust or re‑upload before proceeding.

---

##### Step 4: Project Footprints
Once geometry is successfully loaded into the app, the system will automatically process and query against four Alaska State geography layers:  
- **House Districts**  
- **Senate Districts**  
- **Boroughs**  
- **DOT&PF Regions**

The results will be displayed as a list under **Project Geographies**.  
Carefully review these values to ensure they are correct for your project.  
If the districts or regions look wrong, adjust your geometry and reload before proceeding.
""",

    "Project Information": """
##### Step 1: Select Data Source
Choose how you want to provide project information. You have two options:

- **AASHTOWare Database:** Select from a dropdown list of available projects connected to AASHTOWare.  
  The form will automatically populate with the information stored in the database.  
  Review the pre‑filled details and make any necessary updates.

- **User Input:** Start with a blank form and manually enter all project information.  
  This option is useful if your project is not listed in AASHTOWare or requires custom details.

---

##### Field Indicators
To help guide data entry, fields are marked with the following symbols:

- **⮜** Required fields  
- **●** Fields populated automatically from AASHTOWARE

These indicators appear next to each field label within the form.

---

##### Step 2: Review and Complete Information
Regardless of the data source selected, carefully review the project information.  
Fill out all fields to the best of your ability, ensuring accuracy and completeness.

---

##### Step 3: Submit and Validate
Click **Submit** once the form is complete.  
The system will check that all required fields are filled out and properly formatted.  
If approved, the **Next** option will become available, allowing you to proceed to the following step.
""",

    "Contacts": """
##### Step 1: Add Contact Details
For each project contact, first select the appropriate **role** (e.g., Project Manager, Engineer, Contractor).  
Then provide the available details such as **name, email, and phone number**.  
If some fields are not applicable, fill in what you have.

---

##### Step 2: Add to Contact List
Once the information is entered, click **Add Contact**.  
The contact will be added to a running list displayed below.  
You may also remove a contact from the list if needed.

---

##### Step 3: Review and Continue
Ensure all required contacts have been added before proceeding.  
Review the list for accuracy and completeness.  
When finished, continue to the next step in the workflow.
""",

    "Review": """
##### Step 1: Review Project Summary
This page provides a complete summary of everything entered throughout the workflow.  
Carefully review **all information** before proceeding, as this is your final opportunity to make changes prior to submission.

At the top of the page, the **project name** is displayed, followed by a visual preview of the **project footprint** on the map.  
Confirm that the footprint accurately represents the project location, route, or boundary and matches how the project should appear publicly.

---
##### Step 2: Review Project Footprint
The **Project Footprint** section displays your submitted geometry directly on the map:

- **Site Projects:** Shown as a point or clustered points  
- **Route Projects:** Shown as one or more line segments  
- **Boundary Projects:** Shown as a shaded polygon or multiple combined areas  

Verify that the footprint is complete, correctly placed, and reflects the intended project extent.  
If the footprint is incorrect, use the **JUMP TO SECTION** button to return to the geometry step and make corrections.

---
##### Step 3: Review Project Information
All project information is organized into expandable sections for easier review, including:

- Identification details  
- Timeline and funding information  
- Descriptions and narratives  
- Contact information  
- Project web links  
- Automatically derived geographic districts and regions  

Review each section carefully to ensure values are accurate, complete, and formatted appropriately for public display.

If any information needs to be corrected, select the **JUMP TO SECTION** button within the header to return to the appropriate step and update the data.

---
##### Step 4: Make Edits if Needed
You may return to any previous step at any time using the **JUMP TO SECTION** buttons.  
After making changes, return to the Review tab to confirm the updates before proceeding.

---
##### Step 5: Continue to Final Step
Once all information and the project footprint have been reviewed and confirmed, select **NEXT** to proceed to the final upload step.
""",

    "Upload Project": """
##### Step 1: Begin Upload Process
Once all previous steps are complete and reviewed, click **UPLOAD TO APEX** to begin loading the project into the system.

The upload process runs automatically and sequentially.  
Each step will display a progress indicator and success or failure message as it completes.

---
##### Step 2: Upload Core Project Record
The application first uploads the **primary project record** to the APEX database.

This record establishes the project identity and must succeed before any additional data can be stored.  
If this step fails, the upload process stops immediately and no additional data is uploaded.

A successful upload will display a confirmation message indicating the project was created.

---
##### Step 3: Upload Project Footprint
Next, the system uploads the **project footprint geometry** that was defined earlier in the workflow.

Depending on the project type, this may include:
- Site point geometry  
- Route line geometry  
- Boundary polygon geometry  
- Multiple geometries if applicable  

Each geometry is uploaded individually.  
If any geometry fails to upload, the system records the error and reports the failure.

---
##### Step 4: Upload Project Geographies
After the footprint is uploaded, the application processes and uploads **derived geography records**, including:
- DOT&PF Regions  
- Boroughs or Census Areas  
- House Districts  
- Senate Districts  

These records are generated automatically based on the submitted footprint.  
If any geography layer fails to load, the failure will be reported while allowing other layers to continue processing.

---
##### Step 5: Final Background Processing
Once the main uploads are complete, the system performs additional background steps, including:
- Updating the project location record  
- Initializing Traffic Impact records if applicable  

These steps run automatically and do not require user interaction.

Errors encountered during these background steps are recorded and reported as part of the final upload summary.

---
##### Step 6: Review Upload Results
If **any step fails**, the upload is marked as unsuccessful and a detailed list of failures is displayed.

When possible, the system will attempt to **clean up any partially uploaded data** to prevent incomplete projects from remaining in the database.

If **all steps succeed**, a success confirmation is displayed indicating the project was fully uploaded to APEX.

---
##### Step 7: Choose Next Action
After a successful upload, select one of the following options:

- **RETURN TO LOADER**  
  Resets the application and returns you to the project loader to begin entering a new project.

- **MANAGE PROJECT**  
  Opens the newly uploaded project in the management interface for further editing or maintenance.

Select the option that best matches your next task to complete the workflow.
""",

    "Other Tab Example": """
#### Instructions
Add instructions for other tabs here as needed.
"""
}




def instructions(tab_name: str):
    """
    Display instructions for a given tab inside a Streamlit expander.
    """
    content = TAB_INSTRUCTIONS.get(tab_name)
    if content:
        with st.expander("Instructions", expanded=False):
            st.markdown(content)
    else:
        st.warning(f"No instructions found for tab: {tab_name}")
