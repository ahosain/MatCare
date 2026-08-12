#!/usr/bin/env python
# coding: utf-8

# In[ ]:


# 2. Filter df2 where 'Name' exists in df1's 'FAC_Name' column
matched_df = df2[df2['NAME'].isin(df1['FAC_NAME'])]

# 3. Save the result to a new CSV file
matched_df.to_csv('texas_obs_fac.csv', index=False)

# View the first few rows of the result
print(matched_df.head())


# The file was downloaded from this link: https://source.coop/seerai/hifld/hospitals/hospitals/hospitals.parquet

# In[ ]:



import pandas as pd

# Define the path to your downloaded file
file_path = 'test_HIFLD.parquet'

try:
    # Read the parquet file
    df = pd.read_parquet(file_path)

    # Display basic information about the dataset
    print("File loaded successfully!")
    print(f"Rows: {df.shape[0]}, Columns: {df.shape[1]}")

    # Show the first 5 rows
    print(df.head())

    # Optional: Filter for Texas immediately if this is the HIFLD data
    if 'STATE' in df.columns:
        df_tx = df[df['STATE'] == 'TX']
        print(f"Filtered to {len(df_tx)} Texas records.")

except Exception as e:
    print(f"Error reading the parquet file: {e}")


# In[ ]:


df.info()


# # Only looking at Texas data

# In[ ]:


df_tx = df[df['STATE'] == 'TX']
print(df_tx.head(1))


# In[ ]:


# The number of rows and columns in Texas data
print(f"Rows: {df_tx.shape[0]}, Columns: {df_tx.shape[1]}")


# In[ ]:


# Save the Texas subset to a CSV file
df_tx.to_csv('texas_hospitals_subset.csv', index=False)

print("Success! Your file has been saved as 'texas_hospitals_subset.csv'.")


# # Cross-reference CMOS data with Texas Data to find Obstetric facilities

# In[ ]:


import pandas as pd

# Load your CSV files
df1 = pd.read_csv('Cleaned_CMOS_Data.csv')
df2 = pd.read_csv('Cleaned_texas_hospitals_HIFLD.csv')


# In[ ]:


print(df1.head())


# In[ ]:


print(df2.head())


# In[ ]:


# 2. Filter df2 where 'Name' exists in df1's 'FAC_Name' column
matched_df = df2[df2['NAME'].isin(df1['FAC_NAME'])]

# 3. Save the result to a new CSV file
matched_df.to_csv('texas_obs_fac.csv', index=False)

# View the first few rows of the result
print(matched_df.head())


# ## Adding obstetric service levels

# In[ ]:


import pandas as pd

# Load the texas_obs_fac.csv file
df_texas_obs_fac = pd.read_csv('texas_obs_fac.csv')

# Ensure df1 (Cleaned_CMOS_Data.csv) is loaded, if not already.
# Assuming df1 is already loaded from previous cells. If not, uncomment the line below:
# df1 = pd.read_csv('Cleaned_CMOS_Data.csv')

# Merge df_texas_obs_fac with df1 to bring in the 'OB_SRVC_CD' column
# We'll merge on 'NAME' from df_texas_obs_fac and 'FAC_NAME' from df1.
# A left merge will keep all records from df_texas_obs_fac.
combined_df = pd.merge(df_texas_obs_fac, df1[['FAC_NAME', 'OB_SRVC_CD']],
                     left_on='NAME', right_on='FAC_NAME', how='left')

# Drop the redundant 'FAC_NAME' column that came from df1 after the merge
combined_df.drop(columns=['FAC_NAME'], inplace=True, errors='ignore')

# Remove any potential duplicate rows that might have been introduced by the merge
original_rows = combined_df.shape[0]
combined_df.drop_duplicates(inplace=True)
print(f"Removed {original_rows - combined_df.shape[0]} duplicate rows.")

# Save the result to a new CSV file
output_filename = 'texas_obs_fac_with_ob_srvc.csv'
combined_df.to_csv(output_filename, index=False)

print(f"Successfully created '{output_filename}' with OB_SRVC_CD column.")
print("Here are the first few rows of the new combined dataframe:")
print(combined_df.head())


# In[ ]:


print('Column names for df1:')
print(df1.columns.tolist())


# In[ ]:


print('\nColumn names for df2:')
print(df2.columns.tolist())


# In[ ]:


print('Column names for df2:')
print(df2.columns.tolist())


# In[ ]:





# # **Shape file for TX counties**

# In[ ]:


import geopandas as gpd

# URL for the US Census 2023 County Shapefile (500k resolution, which is perfect for mapping)
census_url = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"

print("Downloading US County boundaries...")
# Geopandas can read zip URLs directly!
us_counties = gpd.read_file(census_url)

# Texas's State FIPS code is '48'
# The Census uses 'STATEFP' or 'STATEFP20' depending on the version
state_col = 'STATEFP' if 'STATEFP' in us_counties.columns else 'STATEFP20'

print("Filtering for Texas...")
tx_counties = us_counties[us_counties[state_col] == '48']

# Save it to your computer as a Shapefile bundle
tx_counties.to_file("texas_counties_shapefile.shp")
print("Saved successfully! 'texas_counties_shapefile.zip' (and its sidecars) are ready.")


# ## Using Lat-long to find which hospital falls under what county (Not needed since the original file already have County info)

# In[ ]:


import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

# 1. Load your Texas shapefile
# (Ensure all sidecar files like .shx, .dbf, and .prj are in the same folder)
counties = gpd.read_file("texas_counties_shapefile.shp")

# 2. Load your hospital CSV data
hospitals_df = pd.read_csv("texas_obs_fac.csv")

# 3. Convert the Pandas DataFrame into a GeoDataFrame
# We create a 'geometry' column using the Longitude and Latitude.
# WARNING: Point takes (Longitude, Latitude) in that exact order: (X, Y)
geometry = [Point(xy) for xy in zip(hospitals_df['LONGITUDE'], hospitals_df['LATITUDE'])]

# WGS84 (EPSG:4326) is the standard coordinate system for raw Lat/Long
hospitals_gdf = gpd.GeoDataFrame(hospitals_df, geometry=geometry, crs="EPSG:4326")

# 4. Align coordinate reference systems (CRS)
# The shapefile and the points MUST be in the same projection to match properly.
if counties.crs != hospitals_gdf.crs:
    counties = counties.to_crs(hospitals_gdf.crs)

# 5. Perform the Spatial Join ('sjoin')
# 'predicate="within"' checks if the hospital point is WITHIN a county polygon
joined_df = gpd.sjoin(hospitals_gdf, counties, how="left", predicate="within")

# 6. Save your new dataset
# joined_df will now contain all your original hospital data PLUS the
# county columns from the shapefile (like GEOID, County Name, etc.)
joined_df.to_csv("hospitals_with_counties.csv", index=False)

print("Spatial join complete! Check 'hospitals_with_counties.csv'.")

