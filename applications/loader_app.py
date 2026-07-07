
def run_loader_app():
    import streamlit as st
    from streamlit_folium import st_folium
    from streamlit_scroll_to_top import scroll_to_here
    import folium

    from util.map_util import add_small_geocoder
    from steps.details_form import project_details_form
    from util.instructions_util import instructions
    from steps.review import review_information
    from steps.load_project import load_project_apex
    from steps.load_geometry import load_geometry_app
    from agol.agol_util import get_assignee_submitter_list

    from util.container_util import (
        header_markdown,
        subheader_markdown,
        section_markdown,
        title_markdown
    )


    # Base overview map
    m = folium.Map(location=[64.2008, -149.4937], zoom_start=4)
    add_small_geocoder(m)


    st.set_page_config(
        page_title="APEX Loader Application",
        page_icon="📝",
        layout="centered",
        initial_sidebar_state="collapsed"  # 👈 auto-collapse
    )


    TOTAL_STEPS = 5
    if "loader_step" not in st.session_state:
        st.session_state.loader_step = 1

    # --- Initialize scroll flags ---
    if "scroll_to_top" not in st.session_state:
        st.session_state.scroll_to_top = False

    # --- Handle scroll action ---
    if st.session_state.scroll_to_top:
        scroll_to_here(0, key="top")  # 0 = instant scroll
        st.session_state.scroll_to_top = False  # reset after scrolling

    # --- Navigation functions ---
    def next_step():
        if st.session_state.loader_step < TOTAL_STEPS:
            st.session_state.loader_step += 1
        st.session_state.scroll_to_top = True  # trigger scroll

    def prev_step():
        if st.session_state.loader_step > 1:
            st.session_state.loader_step -= 1
        st.session_state.scroll_to_top = True  # trigger scroll



    # Header and progress
    title_markdown("ADD A NEW APEX PROJECT")
    st.markdown("##### COMPLETE STEPS TO ADD A NEW PROJECT TO THE APEX DATABASE")
    st.progress(st.session_state.loader_step / TOTAL_STEPS)
    st.caption(f"Step {st.session_state.loader_step} of {TOTAL_STEPS}")
    st.write("")

    # Step content
    if st.session_state.loader_step == 1:
        st.header("Welcome")
        st.write("""
            ##### Alaska DOT&PF APEX Project Creator

            Follow these steps to create a new project in the system:

            **Step 1: Enter Project Information**  
            Provide project details either by pulling data from the AASHTOWare database or entering them manually.  
            Review and complete all required fields to ensure accuracy.

            ---

            **Step 2: Upload Project Footprint**  
            Select the project type (**Site**, **Route**, or **Boundary**) and upload or create the corresponding geometry.  
            Choose the upload method that best matches your data (shapefile, coordinates, or map input).  
            Verify that the geometry is correct and reflects your project scope.

            ---

            **Step 3: Review and Confirm**  
            Check all project information, contacts, and geospatial data for completeness and accuracy.  
            Make any adjustments before finalizing.

            ---

            **Step 4: Submit Project**  
            Click **Submit** to validate the data.  
            Once approved, the project will be saved to the database and you can proceed to the next workflow stage.
            """)

        st.info("Click **Next** to begin.")


    elif st.session_state.loader_step == 2:
        st.markdown("### PROJECT INFORMATION 📄")
        st.write(
        "Choose either the AASHTOWare Database or User Input option to provide project details. "
        "Complete the form, then click **Submit Information**, this will check to see if all required values are present.  If"
        " all information is present and in the correct format, you will be able to continue"
        )

        instructions("Project Information")

        st.write('')

        project_details_form()
        

    elif st.session_state.loader_step == 3:
        st.markdown("### LOAD FOOTPRINT 📍")
        st.write(
            "Select the project type and provide its footprint. "
            "After choosing a type, you will see the available upload methods. "
            "Review the instructions below for detailed guidance before continuing."
        )

        instructions("Load Geometry")

        st.write("")
        st.write("")
        
        load_geometry_app()



    elif st.session_state.loader_step == 4:
        st.markdown("### REVIEW PROJECT ✔️")
        st.write(
        "Review all submitted project information carefully. "
        "Confirm details are correct before pressing Submit. "
        "Once submitted, the project will be loaded into the APEX Database.")

        instructions("Review")

        st.write("")
        st.write("")

        review_information()



    elif st.session_state.loader_step == 5:
        st.markdown("### UPLOAD PROJECT🚀")
        st.write(
            "Select your name from the dropdown. If not listed, choose **Other** and enter it in the text box. "
            "Then click **UPLOAD TO APEX** to transfer your project data. "
            "Each step shows a success message if completed, or errors to fix if it fails. "
            "Once all steps succeed, your project will be stored in the APEX Database."
        )

        instructions("Upload Project")

        st.write("")
        st.write("")

        # Display Drop Down of Uploaders
        st.markdown("<h5>Submitter Name</h5>", unsafe_allow_html=True)

        uploaders = get_assignee_submitter_list()
        selected_name = st.selectbox("Submitted by:", uploaders, index=0)

        # If "Other" is chosen, show a text box to override
        if selected_name == "Other":
            custom_name = st.text_input("Please type your name:")

            if custom_name.strip():
                st.session_state['submitted_by'] = custom_name.strip()

        else:
            # ✅ Strip org if present: "ORG – Name" → "Name"
            if "–" in selected_name:
                st.session_state['submitted_by'] = selected_name.split("–", 1)[1].strip()
            else:
                st.session_state['submitted_by'] = selected_name.strip()

        st.write("")

        # Upload Project Option Once Submitter Loaded
        st.markdown("<h5>Upload Project</h5>", unsafe_allow_html=True)

        # ✅ Back + Upload buttons appear together BEFORE upload starts
        col_back, col_gap, col_upload, _ = st.columns([1.5, 0.2, 3, 6])

        if not st.session_state.get("upload_clicked", False):

            # Back button
            with col_back:
                st.button("⬅️ Back", on_click=prev_step, key="step6_back_btn")

            # Upload button
            if st.session_state.get('submitted_by'):
                with col_upload:
                    if st.button("UPLOAD TO APEX", type="primary", key="step6_upload_btn"):
                        st.session_state.upload_clicked = True
                        st.rerun()

        else:
            # Hide buttons once upload starts
            with col_back:
                st.empty()
            with col_upload:
                st.empty()

            # --- Upload Button Logic (unchanged) ---
            if st.session_state.get("upload_clicked", False):
                load_project_apex()





    # -------------------------------------------------------------------------
    # Navigation controls
    # -------------------------------------------------------------------------
    st.write("")
    cols = st.columns([1, 1])

    step = st.session_state.loader_step

    # ✅ ALL STEPS EXCEPT STEP 5
    if step != 5:

        # Back button
        with cols[0]:
            st.button("❮ Back", on_click=prev_step, disabled=step == 1, use_container_width=True, type = 'primary')

        # Next button logic
        with cols[1]:
            can_proceed = False

            if step == 1:
                can_proceed = True
            elif step == 2:
                can_proceed = st.session_state.get("details_complete", False)
            elif step == 3:
                can_proceed = st.session_state.get("footprint_submitted", False)
            elif step == 4:
                can_proceed = True
            if step < TOTAL_STEPS:
                st.button("Next ❯", on_click=next_step, disabled=not can_proceed, use_container_width=True, type = 'primary')

        st.caption("Use Back and Next to navigate. Refresh will reset this session.")




