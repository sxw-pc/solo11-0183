def aggregate(rows):
    return {'holders':len(rows),'animal_hours':sum(row['animal_hours'] for row in rows)}
